"""Reject builder-specific CPU requirements in generated Linux engine commands."""
import json
from pathlib import Path
import shlex
import sys


def check_commands(commands):
    if not commands:
        raise ValueError('Engine compile commands are empty')
    # x86-64-v3 retains AVX2/FMA CPU paths without requiring AVX-512 or
    # adopting the instruction set of whichever CPU builds the image.
    allowed_flags = {
        '-march=x86-64', '-march=x86-64-v2', '-march=x86-64-v3', '-mtune=generic',
        '-m64', '-mmmx', '-msse', '-msse2', '-msse3', '-mssse3', '-msse4.1',
        '-msse4.2', '-mcx16', '-msahf', '-mpopcnt', '-mavx', '-mavx2', '-mfma',
        '-mf16c', '-mbmi', '-mbmi2', '-mlzcnt', '-mmovbe', '-mxsave',
    }
    for entry in commands:
        tokens = entry.get('arguments') or shlex.split(entry['command'])
        # nvcc may forward comma-separated host flags with -Xcompiler=.
        for token in tokens:
            for flag in token.removeprefix('-Xcompiler=').split(','):
                if flag.startswith('-m') and flag not in allowed_flags and not flag.startswith('-mno-'):
                    raise ValueError(f"CPU flag exceeds the portable baseline: {flag} in {entry['file']}")


if __name__ == '__main__':
    check_commands(json.loads(Path(sys.argv[1]).read_text()))
