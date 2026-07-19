"""最小化测试：服务能否启动并响应 /api/models。"""
import sys, os, subprocess, time, threading, urllib.request

PORT = 19531
env = os.environ.copy()
env["AGENT_OS_BACKEND"] = "codebuddy-sdk"

proc = subprocess.Popen(
    [sys.executable, ".agent_os/main.py", "--port", str(PORT),
     "--backend", "codebuddy-sdk", "--no-browser"],
    cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    env=env,
    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    stdin=subprocess.DEVNULL,
)

# 读日志
log = []
def reader(pipe, label):
    for line in iter(pipe.readline, b""):
        log.append(f"[{label}] {line.decode('utf-8','replace').rstrip()}")
threading.Thread(target=reader, args=(proc.stdout, "O"), daemon=True).start()
threading.Thread(target=reader, args=(proc.stderr, "E"), daemon=True).start()

print("Waiting for server...")
for i in range(30):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/models", timeout=2)
        print("SUCCESS: Server is up!")
        proc.terminate()
        proc.wait(timeout=5)
        sys.exit(0)
    except Exception:
        time.sleep(1)

print("FAILED. Server log:")
for l in log[-30:]:
    print(l)
proc.kill()
sys.exit(1)
