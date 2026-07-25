import subprocess

result = subprocess.run(['netstat', '-ano'], capture_output=True, text=True)
pid = None
for line in result.stdout.splitlines():
    if '14876' in line and 'LISTENING' in line:
        pid = line.strip().split()[-1]
        break

if not pid:
    print('Server not running (no process on port 14876)')
else:
    print(f'Stopping server PID={pid}...')
    r = subprocess.run(['taskkill', '/f', '/pid', pid], capture_output=True, text=True)
    if r.returncode == 0:
        print(f'Server PID={pid} stopped')
    else:
        print(f'Failed: {r.stderr.strip()}')
