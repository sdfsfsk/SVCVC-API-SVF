import os
import sys
import json
import logging
import queue
import re
import subprocess
import time
import threading

logger = logging.getLogger("msst_separate")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RVC_API_DIR = os.path.dirname(SCRIPT_DIR)
LOCAL_MSST_DIR = SCRIPT_DIR

MSST_PYTHON = None
_parent = os.path.dirname(RVC_API_DIR)
_candidates = []

def _get_torch_ver(ver_file):
    if not os.path.isfile(ver_file):
        return ""
    try:
        with open(ver_file, "r") as _vf:
            for _line in _vf:
                _s = _line.strip()
                if _s.startswith("__version__"):
                    _val = _s.split("=", 1)[-1].strip().strip("'\"")
                    return _val
    except Exception:
        pass
    return ""

def _scan_conda(base_dir, depth=0):
    _p = os.path.join(base_dir, ".conda", "python.exe")
    if os.path.isfile(_p):
        _ver_file = os.path.join(base_dir, ".conda", "Lib", "site-packages", "torch", "version.py")
        _ver = _get_torch_ver(_ver_file)
        _zluda = os.path.isdir(os.path.join(base_dir, ".conda", "zluda"))
        _candidates.append((_p, _ver, _zluda))
    if depth < 2:
        try:
            for _d in os.listdir(base_dir):
                _sub = os.path.join(base_dir, _d)
                if os.path.isdir(_sub) and not _d.startswith("."):
                    _scan_conda(_sub, depth + 1)
        except Exception:
            pass

_scan_conda(_parent)

for _pref_ver in ["2.2", "2.3"]:
    for _p, _ver, _zluda in _candidates:
        if _zluda and _ver.startswith(_pref_ver):
            MSST_PYTHON = _p
            break
    if MSST_PYTHON:
        break

if MSST_PYTHON is None:
    for _p, _ver, _zluda in _candidates:
        if _zluda:
            MSST_PYTHON = _p
            break

if MSST_PYTHON is None and _candidates:
    MSST_PYTHON = _candidates[0][0]

if MSST_PYTHON is None:
    MSST_PYTHON = os.path.join(RVC_API_DIR, ".conda", "python.exe")

# Prefer the local native ROCm Python 3.12 runtime.  The legacy environment
# scan above remains available as a fallback when runtime-rocm is absent.
NATIVE_ROCM_PYTHON = os.path.join(RVC_API_DIR, "runtime-rocm", "Scripts", "python.exe")
if os.path.isfile(NATIVE_ROCM_PYTHON):
    MSST_PYTHON = NATIVE_ROCM_PYTHON
MSST_ROOT = LOCAL_MSST_DIR
DEFAULT_MODEL_PATH = os.path.join(MSST_ROOT, "pretrain", "vocal_models", "model_bs_roformer_ep_317_sdr_12.9755.ckpt")
DEFAULT_CONFIG_PATH = os.path.join(MSST_ROOT, "configs", "vocal_models", "model_bs_roformer_ep_317_sdr_12.9755.ckpt.yaml")
DEFAULT_MODEL_ID = "bs_roformer_ep_317_sdr_12.9755"

MSST_MODEL_REGISTRY = {
    DEFAULT_MODEL_ID: {
        "display_name": "BS-Roformer Vocals (SDR 12.9755)",
        "model_path": DEFAULT_MODEL_PATH,
        "config_path": DEFAULT_CONFIG_PATH,
    },
    "bs_roformer_karaoke_frazer_becruily": {
        "display_name": "BS-Roformer Karaoke (Frazer/Becruily)",
        "model_path": os.path.join(
            MSST_ROOT, "pretrain", "vocal_models", "bs_roformer_karaoke_frazer_becruily.ckpt"
        ),
        "config_path": os.path.join(
            MSST_ROOT, "configs", "vocal_models", "config_karaoke_frazer_becruily.yaml"
        ),
    },
}


def get_msst_models():
    """Return installed, selectable MSST models for the Gradio API."""
    return [
        {
            "id": model_id,
            "name": spec["display_name"],
        }
        for model_id, spec in MSST_MODEL_REGISTRY.items()
        if os.path.isfile(spec["model_path"]) and os.path.isfile(spec["config_path"])
    ]


def resolve_msst_model(model_name=None):
    """Resolve a public model id to trusted local checkpoint/config paths."""
    requested = str(model_name or DEFAULT_MODEL_ID).strip()
    if requested in MSST_MODEL_REGISTRY:
        model_id = requested
    else:
        requested_lower = requested.lower()
        matches = [
            model_id
            for model_id, spec in MSST_MODEL_REGISTRY.items()
            if requested_lower == spec["display_name"].lower()
        ]
        if not matches:
            raise ValueError(
                f"Unknown MSST model: {requested}. Available: {', '.join(MSST_MODEL_REGISTRY)}"
            )
        model_id = matches[0]

    spec = MSST_MODEL_REGISTRY[model_id]
    if not os.path.isfile(spec["model_path"]):
        raise FileNotFoundError(f"[MSST] Model not found: {spec['model_path']}")
    if not os.path.isfile(spec["config_path"]):
        raise FileNotFoundError(f"[MSST] Config not found: {spec['config_path']}")
    return model_id, spec["model_path"], spec["config_path"]

_separator_instance = None
_current_model_path = None


def _generate_subprocess_script(params, output_dir):
    params_json = json.dumps(params, ensure_ascii=False, indent=2)
    script = f'''import sys
import os
import time
import json
import traceback

sys.path.insert(0, {repr(LOCAL_MSST_DIR)})
sys.path.insert(0, {repr(MSST_ROOT)})


class SimpleProgress:
    def __init__(self, total=None, desc="", leave=False, **kwargs):
        self.total = total or 0
        self.desc = desc
        self.n = 0
        self._last_pct = -1

    def update(self, n=1):
        self.n += n
        if self.total > 0:
            pct = int(self.n / self.total * 100)
            if pct != self._last_pct and pct % 5 == 0:
                self._last_pct = pct
                print(f"[MSST_SUB] {{self.desc}}: {{pct}}%", flush=True)

    def close(self):
        if self.total > 0 and self._last_pct < 100:
            print(f"[MSST_SUB] {{self.desc}}: 100%", flush=True)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def refresh(self, *args, **kwargs):
        pass

    def reset(self, total=None):
        if total is not None:
            self.total = total
        self.n = 0
        self._last_pct = -1

    def set_description(self, desc=None):
        if desc:
            self.desc = desc

    def set_postfix(self, **kwargs):
        pass

    @property
    def format_dict(self):
        return {{"n": self.n, "total": self.total}}


import tqdm as _tqdm_mod
import tqdm.auto as _tqdm_auto
import tqdm.std as _tqdm_std
import tqdm.autonotebook as _tqdm_an
import tqdm.asyncio as _tqdm_async
_tqdm_mod.tqdm = SimpleProgress
_tqdm_auto.tqdm = SimpleProgress
_tqdm_std.tqdm = SimpleProgress
_tqdm_an.tqdm = SimpleProgress
_tqdm_async.tqdm = SimpleProgress
print("[MSST_SUB] tqdm patched -> SimpleProgress", flush=True)

import os as _os
import sys as _sys
_conda_dir = _os.path.dirname(_os.path.abspath(_sys.executable))
_zluda_dir = _os.path.join(_conda_dir, "zluda")
_rocm6_dir = _os.path.join(_zluda_dir, "rocm6")
_rocm5_dir = _os.path.join(_zluda_dir, "rocm5")
_native_rocm = _os.environ.get("RVC_NATIVE_ROCM") == "1" or "runtime-rocm" in _os.path.normcase(_sys.executable)

if _os.path.isdir(_rocm6_dir):
    _os.environ["PATH"] = _rocm6_dir + ";" + _rocm5_dir + ";" + _zluda_dir + ";" + _conda_dir + ";" + _os.environ.get("PATH", "")
    _sys.path.insert(0, _conda_dir)
    _sys.path.insert(0, _zluda_dir)
    print("[MSST_SUB] ZLUDA dirs added to PATH and sys.path", flush=True)

import ctypes as _ctypes
_nvcuda_path = _os.path.join(_rocm6_dir, "nvcuda.dll")
if _os.path.exists(_nvcuda_path):
    try:
        _ctypes.WinDLL(_nvcuda_path)
        print("[MSST_SUB] Preloaded nvcuda.dll (CUDA->ROCm translation)", flush=True)
    except Exception as _e:
        print(f"[MSST_SUB] Warning: nvcuda.dll preload failed: {{_e}}", flush=True)

if not _native_rocm:
    _os.environ["nv"] = "1"
    print("[MSST_SUB] Set nv=1 (skip auto-init, call zludainit manually before torch)", flush=True)
else:
    _os.environ.setdefault("MIOPEN_LOG_LEVEL", "3")
    print("[MSST_SUB] Native Windows ROCm runtime selected", flush=True)

use_zluda = False
if _os.path.isdir(_zluda_dir):
    try:
        from zluda import zludainit as _zludainit
        print("[MSST_SUB] Calling zludainit.init() BEFORE import torch...", flush=True)
        _tmp = _zludainit.init()
        _zluda_running = _tmp[0]
        _zluda_gfx = _tmp[1]
        _zluda_rocmv = _tmp[2]
        if _zluda_running:
            use_zluda = True
            print(f"[MSST_SUB] ZLUDA initialized: gfx={{_zluda_gfx}}, rocmv={{_zluda_rocmv}}", flush=True)
        else:
            print("[MSST_SUB] zludainit.init() returned not running", flush=True)
    except Exception as _e:
        use_zluda = False
        print("[MSST_SUB] zludainit.init() failed, using CPU", flush=True)

import torch

if _native_rocm:
    print(f"[MSST_SUB] PyTorch {{torch.__version__}}, HIP {{torch.version.hip}}", flush=True)

if use_zluda:
    if torch.version.hip is not None:
        torch.version.hip = None

    def _safe_raw_device_count():
        return -1
    def _safe_device_count():
        return -1
    try:
        import torch.cuda as _cuda_mod
        _cuda_mod._raw_device_count_amdsmi = _safe_raw_device_count
        _cuda_mod._device_count_amdsmi = _safe_device_count
    except Exception:
        pass

    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False

_gpu_device = "cpu"
if (_native_rocm or use_zluda) and torch.cuda.is_available():
    _gpu_device = "cuda:0"
    try:
        _gpu_name = torch.cuda.get_device_name(0)
        _backend = "native ROCm" if _native_rocm else "ZLUDA"
        print(f"[MSST_SUB] GPU: {{_gpu_name}} ({{_backend}})", flush=True)
    except Exception:
        print("[MSST_SUB] AMD GPU detected", flush=True)
else:
    print("[MSST_SUB] Using CPU for separation", flush=True)

import librosa
import soundfile as sf
import numpy as np

from inference.msst_infer import MSSeparator

import utils.utils as _utils_mod
_utils_mod.tqdm = SimpleProgress
print("[MSST_SUB] utils.tqdm patched -> SimpleProgress", flush=True)

def main():
    try:
        params = json.loads({repr(params_json)})

        model_path = params["model_path"]
        config_path = params["config_path"]
        input_audio = params["input_audio"]
        output_dir = params["output_dir"]
        output_format = params.get("output_format", "wav")
        inference_params = dict(params.get("inference_params") or {{}})
        use_tta = bool(inference_params.pop("use_tta", False))

        os.makedirs(output_dir, exist_ok=True)

        sep = MSSeparator(
            model_type="bs_roformer",
            config_path=config_path,
            model_path=model_path,
            device=_gpu_device,
            output_format=output_format,
            store_dirs=output_dir,
            use_tta=use_tta,
            inference_params=inference_params,
        )

        if "cuda" in str(sep.device):
            _backend = "native ROCm" if _native_rocm else "ZLUDA"
            print(f"[MSST_SUB] {{_backend}}: Model stays FP32", flush=True)

        print(f"[MSST_SUB] Model loaded, device: {{sep.device}}, dtype: {{next(sep.model.parameters()).dtype}}", flush=True)

        mix, sr = librosa.load(input_audio, sr=44100, mono=False)
        print(f"[MSST_SUB] Audio: {{mix.shape}}, sr={{sr}}", flush=True)

        start = time.time()
        results = sep.separate(mix)
        elapsed = time.time() - start
        print(f"[MSST_SUB] Separation done in {{elapsed:.1f}}s", flush=True)

        base_name = os.path.splitext(os.path.basename(input_audio))[0]
        vocal_path = None
        inst_path = None

        for instr, audio in results.items():
            out_file = os.path.join(output_dir, f"{{base_name}}_{{instr}}.{{output_format}}")
            sf.write(out_file, audio, sr, subtype='FLOAT')
            size = os.path.getsize(out_file)
            print(f"[MSST_SUB] Saved {{instr}}: {{out_file}} ({{size/1024:.1f}} KB)", flush=True)

            if instr == "vocals":
                vocal_path = out_file
            elif instr in ("other", "instrumental"):
                inst_path = out_file

        result = {{"vocal_path": vocal_path, "inst_path": inst_path, "elapsed": elapsed}}
        result_file = os.path.join(output_dir, "_msst_result.json")
        with open(result_file, "w") as f:
            json.dump(result, f)
        print(f"[MSST_SUB] Result saved to {{result_file}}", flush=True)

    except Exception as e:
        try:
            msg = f"[MSST_SUB_ERROR] {{type(e).__name__}}: {{e}}"
            print(msg.encode('ascii', 'replace').decode('ascii'), flush=True)
        except Exception:
            print("[MSST_SUB_ERROR] (error message unavailable)", flush=True)
        tb = traceback.format_exc()
        for line in tb.split("\\n"):
            if line.strip():
                try:
                    print(f"[MSST_SUB_ERROR] {{line}}".encode('ascii', 'replace').decode('ascii'), flush=True)
                except Exception:
                    pass
        sys.exit(1)

if __name__ == "__main__":
    main()
'''
    script_path = os.path.join(output_dir, "_msst_run.py")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script)
    return script_path


def _stream_reader(pipe, log_func, prefix="", filter_tqdm=False, progress_events=None):
    last_progress = ""
    try:
        for line in iter(pipe.readline, ""):
            if not line:
                break
            line = line.rstrip("\n\r")
            if not line.strip():
                continue
            if filter_tqdm and "%" in line and "|" in line:
                if line == last_progress:
                    continue
                last_progress = line
            if "[MSST_SUB_ERROR]" in line:
                logger.error(f"{prefix}{line}")
            else:
                log_func(f"{prefix}{line}")
            if progress_events is not None:
                match = re.search(r"\[MSST_SUB\]\s*[^:\r\n]+:\s*(\d{1,3})%", line)
                if match:
                    percent = max(0, min(100, int(match.group(1))))
                    progress_events.put(
                        (percent / 100.0, f"MSST 正在分离目标歌曲 [{percent}%]")
                    )
    except Exception:
        pass
    finally:
        pipe.close()


def separate_vocal_subprocess(input_audio_path, output_dir=None, output_format='wav', model_path=None, config_path=None,
                              inference_params=None, model_name=None, progress_callback=None):
    if model_path is None and config_path is None:
        _, model_path, config_path = resolve_msst_model(model_name)
    else:
        if model_path is None:
            model_path = DEFAULT_MODEL_PATH
        if config_path is None:
            config_path = DEFAULT_CONFIG_PATH

    if not os.path.isfile(model_path):
        raise FileNotFoundError(f"[MSST] Model not found: {model_path}")
    if not os.path.isfile(config_path):
        raise FileNotFoundError(f"[MSST] Config not found: {config_path}")
    if not os.path.isfile(input_audio_path):
        raise FileNotFoundError(f"[MSST] Audio not found: {input_audio_path}")

    input_audio_path = os.path.abspath(input_audio_path)

    if output_dir is None:
        output_dir = os.path.join(os.path.dirname(input_audio_path), "MSST_output")
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    logger.info(f"[MSST] Separating: {os.path.basename(input_audio_path)}")
    _native_runtime = os.path.normcase(os.path.abspath(MSST_PYTHON)) == os.path.normcase(os.path.abspath(NATIVE_ROCM_PYTHON))
    _backend_name = "native ROCm (AMD GPU)" if _native_runtime else "ZLUDA (AMD GPU) / CPU fallback"
    logger.info(f"[MSST] Device: {_backend_name}, Model: {os.path.basename(model_path)}")
    logger.info(f"[MSST] Mode: subprocess (MSST Python: {MSST_PYTHON})")

    params = {
        "model_path": model_path,
        "config_path": config_path,
        "input_audio": input_audio_path,
        "output_dir": output_dir,
        "output_format": output_format,
        "inference_params": inference_params or {},
    }

    script_path = _generate_subprocess_script(params, output_dir)

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = LOCAL_MSST_DIR + ";" + MSST_ROOT

    if _native_runtime:
        env["RVC_NATIVE_ROCM"] = "1"
        env.setdefault("MIOPEN_LOG_LEVEL", "3")

    _conda_dir = os.path.dirname(MSST_PYTHON)
    _zluda_dir = os.path.join(_conda_dir, "zluda")
    _rocm6_dir = os.path.join(_zluda_dir, "rocm6")
    _rocm5_dir = os.path.join(_zluda_dir, "rocm5")
    if not _native_runtime and os.path.isdir(_rocm6_dir):
        env["PATH"] = _rocm6_dir + ";" + _rocm5_dir + ";" + _zluda_dir + ";" + _conda_dir + ";" + env.get("PATH", "")

    logger.info(f"[MSST] Starting subprocess: {MSST_PYTHON}")

    stderr_lines = []
    progress_events = queue.Queue()

    def _drain_progress_events():
        if progress_callback is None:
            return
        while True:
            try:
                value, description = progress_events.get_nowait()
            except queue.Empty:
                break
            try:
                # Run the Gradio callback on the request worker thread. Calling
                # it from the stdout reader thread loses Gradio's progress context.
                progress_callback(value, description)
            except Exception as exc:
                logger.debug(f"[MSST] Progress callback failed: {exc}")

    def _stderr_collector(pipe):
        try:
            for line in iter(pipe.readline, ""):
                if not line:
                    break
                line = line.rstrip("\n\r")
                if line.strip():
                    stderr_lines.append(line)
                    if "[MSST_SUB_ERROR]" in line:
                        logger.error(f"[STDERR] {line}")
                    elif "warning" not in line.lower() and "UserWarning" not in line:
                        logger.warning(f"[STDERR] {line}")
        except Exception:
            pass
        finally:
            pipe.close()

    try:
        proc = subprocess.Popen(
            [MSST_PYTHON, "-u", script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=MSST_ROOT,
            env=env,
            bufsize=1,
        )

        stdout_thread = threading.Thread(
            target=_stream_reader,
            args=(proc.stdout, logger.info, "", True, progress_events),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_stderr_collector, args=(proc.stderr,), daemon=True
        )
        stdout_thread.start()
        stderr_thread.start()

        deadline = time.monotonic() + 600
        while proc.poll() is None:
            _drain_progress_events()
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(proc.args, 600)
            time.sleep(0.1)
        proc.wait(timeout=5)
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        _drain_progress_events()

        if proc.returncode != 0:
            logger.error(f"[MSST] Subprocess failed with code {proc.returncode}")
            if stderr_lines:
                logger.error(f"[MSST] stderr output (last 20 lines):")
                for line in stderr_lines[-20:]:
                    logger.error(f"  {line}")
            raise RuntimeError(f"MSST separation failed with exit code {proc.returncode}")

    except subprocess.TimeoutExpired:
        proc.kill()
        logger.error("[MSST] Subprocess timed out (600s)")
        raise RuntimeError("MSST separation timed out")

    result_file = os.path.join(output_dir, "_msst_result.json")
    if os.path.isfile(result_file):
        with open(result_file, "r") as f:
            result = json.load(f)
        vocal_path = result.get("vocal_path")
        inst_path = result.get("inst_path")
        elapsed = result.get("elapsed", 0)
        logger.info(f"[MSST] Separation completed in {elapsed:.1f}s")
        logger.info(f"[MSST] Vocal: {vocal_path}")
        logger.info(f"[MSST] Instrumental: {inst_path}")
    else:
        base_name = os.path.splitext(os.path.basename(input_audio_path))[0]
        vocal_path = os.path.join(output_dir, f"{base_name}_vocals.{output_format}")
        inst_path = os.path.join(output_dir, f"{base_name}_other.{output_format}")
        if not os.path.isfile(vocal_path):
            vocal_path = None
        if not os.path.isfile(inst_path):
            inst_path = None
        logger.info(f"[MSST] Separation completed (result file not found, using path inference)")

    if vocal_path and os.path.isfile(vocal_path):
        size = os.path.getsize(vocal_path)
        if size < 1000:
            logger.warning(f"[MSST] Vocal file is very small ({size} bytes), might be empty")
    else:
        logger.warning(f"[MSST] Vocal file not found: {vocal_path}")

    return vocal_path, inst_path


def separate_vocal_direct(input_audio_path, output_dir=None, output_format='wav', model_path=None, config_path=None,
                          inference_params=None, release_after=False, model_name=None):
    global _separator_instance, _current_model_path

    effective_inference_params = {
        "batch_size": None,
        "num_overlap": None,
        "chunk_size": None,
        "normalize": None,
        "use_tta": None,
    }
    effective_inference_params.update(inference_params or {})
    use_tta = bool(effective_inference_params.pop("use_tta", False))

    if model_path is None and config_path is None:
        selected_model_id, model_path, config_path = resolve_msst_model(model_name)
    else:
        selected_model_id = str(model_name or os.path.splitext(os.path.basename(model_path or DEFAULT_MODEL_PATH))[0])
        if model_path is None:
            model_path = DEFAULT_MODEL_PATH
        if config_path is None:
            config_path = DEFAULT_CONFIG_PATH

    if not os.path.isfile(model_path):
        raise FileNotFoundError(f"[MSST] Model not found: {model_path}")
    if not os.path.isfile(config_path):
        raise FileNotFoundError(f"[MSST] Config not found: {config_path}")
    if not os.path.isfile(input_audio_path):
        raise FileNotFoundError(f"[MSST] Audio not found: {input_audio_path}")

    input_audio_path = os.path.abspath(input_audio_path)

    if output_dir is None:
        output_dir = os.path.join(os.path.dirname(input_audio_path), "MSST_output")
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    if _separator_instance is None or _current_model_path != model_path:
        previous_model = os.path.basename(_current_model_path) if _current_model_path else "<none>"
        logger.info(
            f"[MSST模型切换Debug] 准备切换分离模型: {previous_model} -> {selected_model_id}"
        )
        logger.info(f"[MSST] Loading model: {selected_model_id} ({os.path.basename(model_path)})")

        _use_cuda = False
        _msst_device = "cpu"
        try:
            import torch
            if torch.cuda.is_available():
                _use_cuda = True
                _msst_device = "cuda:0"
                _dev_name = torch.cuda.get_device_name(0)
                logger.info(f"[MSST] Device: CUDA GPU ({_dev_name}), Mode: direct (in-process)")
            else:
                logger.info("[MSST] No CUDA device available, using CPU")
        except Exception as e:
            logger.info(f"[MSST] CUDA check failed: {e}, using CPU")

        sys.path.insert(0, LOCAL_MSST_DIR)
        sys.path.insert(0, MSST_ROOT)

        from inference.msst_infer import MSSeparator

        _separator_instance = MSSeparator(
            model_type="bs_roformer",
            config_path=config_path,
            model_path=model_path,
            device=_msst_device,
            output_format=output_format,
            store_dirs=output_dir,
            use_tta=use_tta,
            inference_params=effective_inference_params,
        )

        _current_model_path = model_path
        logger.info(f"[MSST] Model loaded, device: {_separator_instance.device}")
        logger.info(
            f"[MSST模型切换Debug] 分离模型切换成功: {selected_model_id} | "
            f"checkpoint={os.path.basename(model_path)} | config={os.path.basename(config_path)}"
        )
    else:
        logger.info(f"[MSST] Reusing cached model")
        logger.info(f"[MSST模型切换Debug] 已在使用分离模型: {selected_model_id}")
        _separator_instance.store_dirs = output_dir
        if inference_params:
            _separator_instance.update_inference_params(_separator_instance.config, effective_inference_params)
        # TTA is request-scoped. Always reset it so a previous enhanced request
        # cannot leak into a later request that reuses the same loaded model.
        _separator_instance.use_tta = use_tta

    logger.info(
        "[MSST] Active inference params: batch_size=%s, overlap=%s, normalize=%s, tta=%s",
        _separator_instance.config.inference.get("batch_size"),
        _separator_instance.config.inference.get("num_overlap"),
        _separator_instance.config.inference.get("normalize", False),
        _separator_instance.use_tta,
    )

    import librosa
    import soundfile as sf

    logger.info(f"[MSST] Separating: {os.path.basename(input_audio_path)}")
    logger.info(
        f"[MSST分离模型Debug] 本次实际分离模型: {selected_model_id} | "
        f"input={os.path.basename(input_audio_path)}"
    )

    mix, sr = librosa.load(input_audio_path, sr=44100, mono=False)
    logger.info(f"[MSST] Audio: {mix.shape}, sr={sr}")

    start = time.time()
    results = _separator_instance.separate(mix)
    elapsed = time.time() - start
    logger.info(f"[MSST] Separation done in {elapsed:.1f}s")

    base_name = os.path.splitext(os.path.basename(input_audio_path))[0]
    vocal_path = None
    inst_path = None

    for instr, audio in results.items():
        instrument_key = str(instr).strip().lower()
        if instrument_key in ("vocal", "vocals"):
            output_label = "vocals"
        elif instrument_key in ("other", "instrumental", "accompaniment"):
            output_label = "other"
        else:
            output_label = str(instr)

        out_file = os.path.join(output_dir, f"{base_name}_{output_label}.{output_format}")
        sf.write(out_file, audio, sr, subtype='FLOAT')
        size = os.path.getsize(out_file)
        logger.info(f"[MSST] Saved {instr}: {out_file} ({size/1024:.1f} KB)")

        if instrument_key in ("vocal", "vocals"):
            vocal_path = out_file
        elif instrument_key in ("other", "instrumental", "accompaniment"):
            inst_path = out_file

    result = (vocal_path, inst_path)
    if release_after:
        unload_model()
    return result


def separate_vocal(input_audio_path, output_dir=None, output_format='wav', model_path=None, config_path=None, mode='direct',
                   inference_params=None, release_after=False, model_name=None, progress_callback=None):
    if mode == 'direct':
        return separate_vocal_direct(input_audio_path, output_dir, output_format, model_path, config_path,
                                     inference_params=inference_params, release_after=release_after,
                                     model_name=model_name)
    else:
        return separate_vocal_subprocess(input_audio_path, output_dir, output_format, model_path, config_path,
                                         inference_params=inference_params, model_name=model_name,
                                         progress_callback=progress_callback)


def separate_vocal_from_array(audio_array, sr=44100, output_dir='./output/MSST', name='audio', mode='subprocess'):
    os.makedirs(output_dir, exist_ok=True)
    temp_path = os.path.join(output_dir, f"_temp_input_{name}.wav")
    if audio_array.ndim == 1:
        audio_array = audio_array.reshape(-1, 1)
    elif audio_array.ndim == 2 and audio_array.shape[0] < audio_array.shape[1]:
        audio_array = audio_array.T
    sf.write(temp_path, audio_array, sr, subtype='FLOAT')

    try:
        vocal_path, inst_path = separate_vocal(temp_path, output_dir=output_dir, output_format='wav', mode=mode)
    finally:
        if os.path.isfile(temp_path):
            os.remove(temp_path)

    return vocal_path, inst_path


def unload_model():
    global _separator_instance, _current_model_path
    if _separator_instance is not None:
        try:
            _separator_instance.del_cache()
        except Exception:
            pass
        _separator_instance = None
        _current_model_path = None
        logger.info("[MSST] Model unloaded from memory")
    else:
        logger.info("[MSST] Model unload (subprocess mode - no persistent model)")


if __name__ == "__main__":
    print("MSST Separator for RVCSVC-API-MSST (DirectML)")
    print(f"Local MSST dir: {LOCAL_MSST_DIR}")
    print(f"MSST Python: {MSST_PYTHON}")
    print(f"MSST Root: {MSST_ROOT}")
    print(f"Default model: {DEFAULT_MODEL_PATH}")
    print(f"Default config: {DEFAULT_CONFIG_PATH}")
