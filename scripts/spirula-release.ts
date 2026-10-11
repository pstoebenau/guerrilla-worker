import path from 'node:path';
import { homedir } from 'node:os';

export const spirulaVersion = '2026.9.30';
export const spirulaDirectory = path.join(homedir(), '.local/share/guerrilla-worker/runtimes', `spirula-${spirulaVersion}`);
// Archive digests are published by the official release; executable digests
// verify the extracted runtime too. This is the entire platform asset matrix.
export const spirulaAssets = {
  'darwin-arm64': {
    name: `spirula-${spirulaVersion}-macos-vulkan-arm64.dmg`,
    archiveSha: '7a43aa51ca60233129e547198cf0c4efe9f46de7e942980753f959b86a2908cc',
    binarySha: '9bd13c9c1077da09d726f9ed8bbf08c5887de9c040ed29de7474eead4afca0cf',
    executable: 'spirula',
  },
  'win32-x64': {
    name: `spirula-${spirulaVersion}-windows-vulkan-x86_64.zip`,
    archiveSha: '79515ab186fc704af6eac22d65365d236a94afde9fa016511cfef2a234e154a9',
    binarySha: 'aaedc3079f640944114c17e748f3356716aa308d6ed1ce84d6794da880dda2fe',
    executable: 'spirula.exe',
  },
  'linux-x64': {
    name: `spirula-${spirulaVersion}-ubuntu-vulkan-x86_64.zip`,
    archiveSha: '123d6d0b826388abb64129b6fcf2a8d34fe0662a57ba34e53212a148c891431d',
    binarySha: '1c4d54bbeda9e72bb9277c6525a949164e1bcdf0377d388dbf008419e9351736',
    executable: 'spirula',
  },
} as const;
