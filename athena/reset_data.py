"""Interactive, allowlisted reset of local test data; no application imports."""
from pathlib import Path
import os
import shutil
import socket


FOLDERS = ('exports', 'changes', 'objects', 'change-sets', 'agent-runs', 'documents')
DATABASES = ('knowledge.sqlite3', 'document_knowledge.sqlite3', 'accounts.sqlite3',
             'agent-tools.sqlite3', 'git/sources.sqlite3')
SETTINGS = ('recent.json', 'connections.json', 'ai.json', 'user.json',
            'completion.json', 'semantic.json')


def targets(directory):
    """Resolve the junction once; reject any nested redirected deletion target."""
    root = Path(directory).resolve(strict=True)
    paths = [root / name for name in FOLDERS + SETTINGS]
    paths += [root / (name + suffix) for name in DATABASES
              for suffix in ('', '-wal', '-shm', '-journal')]
    existing = []
    for path in paths:
        if not path.exists() and not path.is_symlink():
            continue
        if path.resolve() != path or not path.resolve().is_relative_to(root):
            raise ValueError(f'Redirected path: {path}')
        if path.is_dir():
            for folder, dirs, files in os.walk(path, followlinks=False):
                for name in dirs + files:
                    child = Path(folder) / name
                    if child.resolve() != child:
                        raise ValueError(f'Redirected path: {child}')
        existing.append(path)
    return root, existing


def check_stopped():
    for port in range(8765, 8770):
        with socket.socket() as connection:
            connection.settimeout(.3)
            if connection.connect_ex(('127.0.0.1', port)) == 0:
                raise ValueError(f'Close Athena and its reference service first (port {port}).')


def main():
    directory = Path(__file__).resolve().parent.parent / '.athena'
    if not directory.exists():
        print('No local data to reset.')
        return
    root, paths = targets(directory)
    print(f'Data directory: {root}')
    print('DELETE: imports, indexes, drafts, agent history, documents, accounts and settings.')
    print('KEEP: models, GPU/runtime libraries, platform help and Git working copies.')
    print('Original 1C databases and external source folders are not deleted.')
    print('Close ALL Athena instances, reference services and background workers first.')
    print('After reset: run Start.cmd, complete setup; default account admin/admin.')
    for path in paths:
        print('  ' + str(path.relative_to(root)))
    check_stopped()
    if input('Type RESET to permanently delete these data: ').strip() != 'RESET':
        print('Cancelled.')
        return
    check_stopped()
    # Revalidate the entire allowlist immediately before the first deletion.
    _, paths = targets(directory)
    for path in paths:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    print('Reset complete. Run Start.cmd.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyboardInterrupt, EOFError) as exc:
        print(f'Reset stopped: {exc}. No further files will be deleted.')
        raise SystemExit(1)
