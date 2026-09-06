import argparse
import sys
from quota_guard.updater import apply_job

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', required=True)
    try:
        apply_job(parser.parse_args().apply)
    except Exception:
        # apply_job records the outcome; background updates must never show a bootloader error dialog.
        sys.exit(1)
