#!/bin/zsh
set -eu
cd "${0:A:h}"
if [[ ! -x /opt/homebrew/bin/qemu-system-aarch64 ]]; then
    if [[ ! -x /opt/homebrew/bin/brew ]]; then
        echo 'Для Apple Silicon требуется Homebrew. Установите его с https://brew.sh и снова откройте PPTP Bridge.'
        read 'reply?Нажмите Enter для выхода. '
        exit 1
    fi
    /opt/homebrew/bin/brew install qemu
fi
exec /usr/bin/python3 -I setup.py
