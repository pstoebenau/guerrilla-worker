"""Prepare CI updater signing without exposing private keys in logs or source."""
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def configure():
    production = os.environ.get('GITHUB_REF') == 'refs/heads/main'
    private_key = os.environ.get('TAURI_SIGNING_PRIVATE_KEY', '')
    if production:
        if not private_key:
            raise ValueError('Public releases require the TAURI_SIGNING_PRIVATE_KEY repository secret')
        if os.environ.get('GUERRILLA_UPDATER_TEST_PUBKEY'):
            raise ValueError('Public releases cannot use an ephemeral updater key')
        return
    # Fork PRs have no repository secrets. A fresh key still exercises Tauri's
    # real artifact signing; its override is confined to this temporary build.
    directory = Path(os.environ['RUNNER_TEMP']) / 'updater-test-signing'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = directory / 'test.key'
    result = subprocess.run(['bun', 'run', 'tauri', 'signer', 'generate', '--ci',
                             '--password', '', '--write-keys', str(key)],
                            cwd=ROOT / 'desktop', capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('Could not generate the ephemeral CI updater signing key')
    key.chmod(0o600)
    public = key.with_suffix('.key.pub').read_text().strip()
    with open(os.environ['GITHUB_ENV'], 'a', encoding='utf-8') as environment:
        environment.write(f'TAURI_SIGNING_PRIVATE_KEY={key}\nTAURI_SIGNING_PRIVATE_KEY_PASSWORD=\n'
                          f'GUERRILLA_UPDATER_TEST_PUBKEY={public}\n')


if __name__ == '__main__':
    configure()
