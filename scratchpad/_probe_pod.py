import subprocess, os
print("HOSTNAME", os.uname().nodename)
print(subprocess.run(["bash","-lc","nvidia-smi --query-gpu=name,driver_version --format=csv,noheader; df -h /workspace|tail -1; ls /workspace/; du -sh /workspace/hf 2>/dev/null || echo 'no hf'; ls /workspace/venv/bin/vllm 2>/dev/null || echo 'no venv'"],capture_output=True,text=True).stdout)
