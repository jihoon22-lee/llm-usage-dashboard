"""Run every synthetic browser regression; never accepts a live origin."""
from pathlib import Path
import subprocess
import sys

CHECKS = ('smoke','dashboard','features','insights','model_visibility','notifications',
          'quota_hierarchy','review','status','antigravity','privacy','remediation',
          'usability_settings','usability_requests','usability_output')
if __name__ == '__main__':
    for check in CHECKS:
        print(f'Browser check: {check}', flush=True)
        subprocess.run([sys.executable, str(Path(__file__).with_name(f'browser_{check}.py'))], check=True)
