# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Thunder AI (Phase 2 - single-file Windows .exe).

Build:
    pyinstaller thunder_ai.spec --clean
Output:
    dist/ThunderAI.exe

Notes vs. the Phase 2 draft spec: this targets PyInstaller 6.x, which removed
the legacy block_cipher/cipher bytecode-encryption option and the
win_no_prefer_redirects / win_private_assemblies switches, and no longer
passes a.zipfiles to EXE. Bundle contents (datas, hiddenimports, excludes,
windowed one-file EXE named ThunderAI) are unchanged. pathex=['backend'] is
set so the frozen entry point can resolve the "app" package.
"""

from PyInstaller.utils.hooks import collect_submodules

# Dynamic-import-heavy libraries: their registries load submodules with
# importlib, which static analysis cannot see (for example passlib resolves
# passlib.handlers.bcrypt only when CryptContext() is constructed). Collect
# the whole submodule tree for each so the frozen app can resolve them.
_DYNAMIC_PACKAGES = [
    'passlib',
    'jose',
    'chromadb',
    'langgraph',
    'langchain_core',
    'mcp',
]
_DYNAMIC_HIDDEN = []
for _pkg in _DYNAMIC_PACKAGES:
    _DYNAMIC_HIDDEN += collect_submodules(_pkg)

a = Analysis(
    ['backend/launcher.py'],
    pathex=['backend'],
    binaries=[],
    datas=[
        ('frontend/dist', 'frontend/dist'),
        ('backend/skills', 'skills'),
    ],
    hiddenimports=[
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'uvicorn.lifespan.off',
        'chromadb',
        'langgraph',
        'apscheduler',
        'mcp',
        'PIL',
        'google.generativeai',
        'sqlalchemy.dialects.sqlite',
        'pydantic.deprecated.copy_internals',
        'h11',
    ] + _DYNAMIC_HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'scipy', 'numpy.distutils'],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ThunderAI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
    version=None,
)
