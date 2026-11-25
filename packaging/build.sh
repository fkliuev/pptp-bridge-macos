#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
[[ "$(uname -m)" == arm64 ]]
export MACOSX_DEPLOYMENT_TARGET=13.0
out="${1:-$PWD/dist}"
mkdir -p "$out"
out=$(cd "$out" && pwd)
build=$(mktemp -d)
trap 'rm -rf "$build"' EXIT
root="$build/root"
app="$root/Applications/PPTP Bridge.app/Contents"
helpers="$root/Library/PrivilegedHelperTools"
mkdir -p "$app"/{Resources,MacOS} "$helpers" "$root/usr/local/bin" "$build/image"
cp -R vendor/pptp "$build/transport"
make -C "$build/transport" config.h
make -C "$build/transport" -j4 CC=clang pptp
install -m 0555 "$build/transport/pptp" "$helpers/org.pptpbridge.transport"
install -m 0555 packaging/authorize "$helpers/org.pptpbridge.authorize"
install -m 0755 packaging/pptp-bridge "$root/usr/local/bin/pptp-bridge"
cp app/Info.plist "$app"
cp app/{control.py,setup.py,Setup.command} guest/provision.sh LICENSE "$app/Resources"
clang -arch arm64 -fobjc-arc -Wno-deprecated-declarations -framework Cocoa -framework Security app/PPTPBridge.m -o "$app/MacOS/PPTPBridge"
clang -arch arm64 -fobjc-arc -framework Foundation -framework NetFS app/mount-share.m -o "$app/Resources/mount-share"
codesign --force --sign - "$app/Resources/mount-share" "$helpers/org.pptpbridge.transport"
codesign --force --sign - "$(dirname "$app")"
codesign --verify --strict "$(dirname "$app")"
cat > "$build/components.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><array><dict>
<key>RootRelativeBundlePath</key><string>Applications/PPTP Bridge.app</string>
<key>BundleIsRelocatable</key><false/>
<key>BundleIsVersionChecked</key><true/>
<key>BundleOverwriteAction</key><string>upgrade</string>
<key>BundleHasStrictIdentifier</key><true/>
</dict></array></plist>
PLIST
pkgbuild --root "$root" --component-plist "$build/components.plist" --ownership recommended --identifier org.pptpbridge.pkg --version 1.0.0 --install-location / "$build/component.pkg"
cat > "$build/distribution.xml" <<'XML'
<installer-gui-script minSpecVersion="2">
<title>PPTP Bridge</title>
<options customize="never" hostArchitectures="arm64" rootVolumeOnly="true"/>
<allowed-os-versions><os-version min="13.0"/></allowed-os-versions>
<choices-outline><line choice="main"/></choices-outline>
<choice id="main" visible="false"><pkg-ref id="org.pptpbridge.pkg"/></choice>
<pkg-ref id="org.pptpbridge.pkg" version="1.0.0">component.pkg</pkg-ref>
</installer-gui-script>
XML
productbuild --distribution "$build/distribution.xml" --package-path "$build" "$build/image/Install PPTP Bridge.pkg"
cp README.md "$build/image/README.txt"
cp LICENSE "$build/image/LICENSE.txt"
hdiutil create -quiet -ov -format UDZO -fs HFS+ -volname 'PPTP Bridge 1.0.0' -srcfolder "$build/image" "$out/PPTP-Bridge-1.0.0-arm64.dmg"
(cd "$out" && shasum -a 256 PPTP-Bridge-1.0.0-arm64.dmg > SHA256SUMS)
