# -*- mode: python ; coding: utf-8 -*-
import s2protocol.versions
import os.path
block_cipher = None


a = Analysis(['parse-replays.py'],
             pathex=['.'],
             binaries=[],
             datas=[
                 (os.path.join(s2protocol.versions.__path__[0], '*.py'), 's2protocol/versions')
             ],
             hiddenimports=['s2protocol.decoders'],
             hookspath=[],
             runtime_hooks=[],
             excludes=[],
             win_no_prefer_redirects=False,
             win_private_assemblies=False,
             cipher=block_cipher,
             noarchive=False)
pyz = PYZ(a.pure, a.zipped_data,
             cipher=block_cipher)
exe = EXE(pyz,
          a.scripts,
          [],
          exclude_binaries=True,
          name='parse-replays',
          debug=False,
          bootloader_ignore_signals=False,
          strip=False,
          upx=True,
          console=True )
coll = COLLECT(exe,
               a.binaries,
               a.zipfiles,
               a.datas,
               strip=False,
               upx=True,
               upx_exclude=[],
               name='parse-replays')
