"""Prepare an isolated Debian VM and a local, user-specific VPN profile."""
import fcntl
import getpass
import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

RES = Path(__file__).resolve().parent
DATA = Path.home() / "Library/Application Support/PPTP Bridge"
IMAGE = "https://cloud.debian.org/images/cloud/trixie/20261123-2671/debian-13-generic-arm64-20261123-2671.qcow2"
SHA512 = "bf83391b5a7b0c6bb33312befe7b092286b719544dd25d62f4acda627841ad192ab252c32d4e756419c276f8ec6e72dae53c917234475bcc687fc7d6fa919e99"
APP = RES.parent / "MacOS/PPTPBridge"
AUTHORIZE = "/Library/PrivilegedHelperTools/org.pptpbridge.authorize"


def write(path, content):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        f.write(content)
    os.replace(f.name, path)


def read_profile():
    ip = lambda value: str(ipaddress.IPv4Address(value.strip()))
    server = ip(input("IPv4 сервера PPTP: "))
    user = input("Логин VPN: ").strip()
    if not re.fullmatch(r"[A-Za-z0-9_@.\\-]{1,128}", user):
        raise ValueError("Логин VPN содержит неподдерживаемый символ.")
    hosts = [ip(h) for h in input("IPv4 серверов SSH через запятую (Enter — пропустить): ").split(",") if h.strip()]
    smb = input("IPv4 файлового сервера SMB (Enter — пропустить): ").strip()
    if not hosts and not smb:
        raise ValueError("Укажите хотя бы один адрес SSH или SMB.")
    share = smb_user = ""
    if smb:
        smb = ip(smb)
        share = input("Имя общего ресурса SMB: ").strip()
        smb_user = input("Логин SMB: ").strip()
        if not re.fullmatch(r"[\w.$ -]{1,80}", share) or not smb_user or share in (".", ".."):
            raise ValueError("Проверьте имя общего ресурса и логин SMB.")
    return dict(server=server, user=user, hosts=list(dict.fromkeys(hosts)), smb_host=smb,
                share=share, smb_user=smb_user)


def prepare_disk():
    disk = DATA / "debian.qcow2"
    if not disk.exists():
        with tempfile.TemporaryDirectory(dir=DATA) as temp:
            base = Path(temp) / "download"
            print("Загрузка чистого образа Debian…", flush=True)
            subprocess.run(["/usr/bin/curl", "-fL", "--retry", "3", "-o", base, IMAGE], check=True)
            digest = hashlib.sha512()
            with base.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != SHA512:
                raise ValueError("Контрольная сумма Debian не совпала.")
            subprocess.run(["/opt/homebrew/bin/qemu-img", "resize", "-f", "qcow2", base, "8G"], check=True)
            os.replace(base, disk)
    if not (DATA / "uefi-vars.fd").exists():
        shutil.copyfile("/opt/homebrew/share/qemu/edk2-arm-vars.fd", DATA / "uefi-vars.fd")


def prepare_access():
    client = DATA / "vm-key"
    if not client.exists():
        subprocess.run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "guest-access",
                        "-f", client], check=True)
    if not (DATA / "seed.iso").exists():
        with tempfile.TemporaryDirectory(dir=DATA) as temp:
            temp = Path(temp)
            host = temp / "host"
            subprocess.run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "guest-host", "-f", host], check=True)
            seed = temp / "seed"
            seed.mkdir()
            hostpub = host.with_suffix(".pub").read_text().strip()
            cloud = {"users": [{"name": "vpn", "shell": "/bin/bash", "lock_passwd": True,
                                 "sudo": "ALL=(ALL) NOPASSWD:ALL",
                                 "ssh_authorized_keys": [client.with_suffix(".pub").read_text().strip()]}],
                     "ssh_pwauth": False, "ssh_keys": {"ed25519_private": host.read_text(),
                                                       "ed25519_public": hostpub}}
            (seed / "user-data").write_text("#cloud-config\n" + json.dumps(cloud))
            (seed / "meta-data").write_text("instance-id: " + str(uuid.uuid4()) + "\nlocal-hostname: pptp-bridge\n")
            iso = temp / "seed.iso"
            subprocess.run(["/usr/bin/hdiutil", "makehybrid", "-quiet", "-iso", "-joliet",
                            "-default-volume-name", "cidata", "-o", iso, seed], check=True)
            write(DATA / "known_hosts", "pptp-bridge-guest " + hostpub + "\n")
            os.replace(iso, DATA / "seed.iso")
    write(DATA / "vm.ssh", f'''Host gateway
  HostName 127.0.0.1
  Port 33582
  User vpn
  IdentityFile "{client}"
  IdentitiesOnly yes
  BatchMode yes
  HostKeyAlias pptp-bridge-guest
  UserKnownHostsFile "{DATA / 'known_hosts'}"
  GlobalKnownHostsFile /dev/null
  StrictHostKeyChecking yes
  UpdateHostKeys no
  ConnectTimeout 3
  LogLevel ERROR
''')


def provision(profile, control):
    control.boot()
    try:
        control.guest("sudo cloud-init status --wait", check=True)
        if control.guest("test -x /usr/sbin/pppd").returncode:
            package = subprocess.check_output(["/usr/bin/curl", "-fsSL", "--retry", "3",
                "https://deb.debian.org/debian/pool/main/p/ppp/ppp_2.5.2-1+1_arm64.deb"])
            if hashlib.sha256(package).hexdigest() != "442d27c25cfd43b56a23384f6202886c1d8118ff777c9b26ec2b7ca938957141":
                raise ValueError("Контрольная сумма пакета ppp не совпала.")
            control.guest("sudo sh -ec 'cat > /run/pptp-bridge.deb; dpkg -i /run/pptp-bridge.deb; rm /run/pptp-bridge.deb'",
                          input=package, check=True)
        hosts = list(dict.fromkeys(profile["hosts"] + ([profile["smb_host"]] if profile["smb_host"] else [])))
        script = (RES / "provision.sh").read_text().replace("@VPN_USER@", profile["user"].replace("\\", "\\\\"))
        script = script.replace("@ROUTES@", "\n".join(f'/usr/sbin/ip route replace {h}/32 dev "$1"' for h in hosts))
        script = script.replace("@GUARDS@", "\n".join(f"ExecStartPre=/usr/sbin/ip route replace unreachable {h}/32 metric 32767" for h in hosts))
        control.guest("sudo /bin/sh -se", input=script.encode(), check=True)
    finally:
        control.shutdown(announce=False)


def ssh_config(hosts):
    folder = Path.home() / ".ssh"
    folder.mkdir(mode=0o700, exist_ok=True)
    fragment = folder / "pptp-bridge.conf"
    proxy = shlex.quote(str(DATA / "vm.ssh"))
    write(fragment, ("Host " + " ".join(hosts) + f"\n    ProxyCommand /usr/bin/ssh -F {proxy} -W %h:%p gateway\nHost *\n") if hosts else "")
    config = folder / "config"
    target = config.resolve()
    contents = target.read_text() if target.exists() else ""
    include = "Include ~/.ssh/pptp-bridge.conf\n"
    if hosts and include not in contents.splitlines(keepends=True):
        write(target, include + contents)


def main():
    import importlib.util
    os.umask(0o077)
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (DATA / "setup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (DATA / "configured").exists():
            sys.exit("Профиль уже настроен.")
        profile = read_profile()
        for account in (["VPN", "SMB"] if profile["smb_host"] else ["VPN"]):
            password = getpass.getpass(f"Пароль {account}: ")
            if not password or any(ord(ch) < 32 for ch in password) or len(password.encode()) >= 1023:
                raise ValueError("Проверьте пароль.")
            subprocess.run([APP, "--store", account], input=(password + "\n").encode(), check=True)
        write(DATA / "profile.json", json.dumps(profile))
        prepare_disk()
        prepare_access()
        spec = importlib.util.spec_from_file_location("bridge_control", RES / "control.py")
        control = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(control)
        control.RUN.mkdir(mode=0o700, exist_ok=True)
        if control.RUN.lstat().st_uid != os.getuid() or control.RUN.lstat().st_mode & 0o077 or control.RUN.is_symlink():
            raise ValueError("Неверные права временного каталога.")
        provision(profile, control)
        subprocess.run(["/usr/bin/sudo", AUTHORIZE, profile["server"]], check=True)
        ssh_config(profile["hosts"])
        write(DATA / "configured", "1\n")
        print("Готово. Откройте PPTP Bridge для включения VPN.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        sys.exit(str(error))
