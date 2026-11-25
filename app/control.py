import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import sys
import time
from urllib.parse import unquote

RESOURCES = Path(__file__).resolve().parent
HERE = Path.home() / "Library/Application Support/PPTP Bridge"
PROFILE = json.loads((HERE / "profile.json").read_text())
SERVER = str(ipaddress.IPv4Address(PROFILE["server"]))
RUN = Path(f"/private/tmp/pptp-bridge-{os.getuid()}")
READY = RUN / "online"
QMP = RUN / "qmp.sock"
SSH = ["/usr/bin/ssh", "-F", str(HERE / "vm.ssh"), "gateway"]
QEMU = "/opt/homebrew/bin/qemu-system-aarch64"
FIRMWARE = "/opt/homebrew/share/qemu/edk2-aarch64-code.fd"
HOSTS = tuple(str(ipaddress.IPv4Address(h)) for h in PROFILE["hosts"])
SMB_HOST = str(ipaddress.IPv4Address(PROFILE["smb_host"])) if PROFILE["smb_host"] else None
SMB_PORT = 14446
SMB_CONTROL = str(RUN / "smb.ssh")
TRANSPORT = "/Library/PrivilegedHelperTools/org.pptpbridge.transport"
KEYCHAIN = RESOURCES.parent / "MacOS/PPTPBridge"


def guest(command, **kwargs):
    return subprocess.run([*SSH, command], **kwargs)


def qmp_read(stream):
    line = stream.readline()
    if not line:
        raise ConnectionResetError("QEMU закрыл соединение с монитором.")
    return json.loads(line)


def qmp(command):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(4)
        sock.connect(str(QMP))
        with sock.makefile("rwb", buffering=0) as f:
            if "QMP" not in qmp_read(f):
                raise RuntimeError("Неверный ответ монитора QEMU.")
            for name in ("qmp_capabilities", command):
                f.write(json.dumps({"execute": name}).encode() + b"\r\n")
                while True:
                    response = qmp_read(f)
                    if "error" in response:
                        raise RuntimeError(str(response["error"]))
                    if "return" in response:
                        break
            return response["return"]


def running():
    try:
        qmp("query-status")
        return True
    except (ConnectionError, FileNotFoundError):
        try:
            pid = int((RUN / "pid").read_text())
        except (ValueError, FileNotFoundError):
            return False
        process = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="],
                                 capture_output=True, text=True)
        return process.stdout.strip().startswith(QEMU + " -name pptp-bridge-debian ")


def boot():
    if not running():
        READY.unlink(missing_ok=True)
        disk_dir = str(HERE).replace(",", ",,")
        subprocess.run([
            QEMU, "-name", "pptp-bridge-debian", "-machine", "virt,accel=hvf", "-cpu", "host",
            "-smp", "2", "-m", "1024", "-nodefaults", "-display", "none",
            "-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={FIRMWARE}",
            "-drive", f"if=pflash,format=raw,unit=1,file={disk_dir}/uefi-vars.fd",
            "-drive", f"if=virtio,format=qcow2,file={disk_dir}/debian.qcow2",
            "-drive", f"if=none,format=raw,id=seed,readonly=on,file={disk_dir}/seed.iso",
            "-device", "virtio-blk-pci,drive=seed",
            "-netdev", "user,id=lan,hostfwd=tcp:127.0.0.1:33582-:22",
            "-device", "virtio-net-pci,netdev=lan", "-device", "virtio-serial-pci",
            "-chardev", f"socket,id=pptp,path={RUN}/ppp.sock,server=on,wait=off",
            "-device", "virtserialport,chardev=pptp,name=local.pptp",
            "-qmp", f"unix:{QMP},server=on,wait=off", "-serial", f"file:{RUN}/console.log",
            "-pidfile", str(RUN / "pid"), "-daemonize"], check=True)
    for _ in range(90):
        if not guest("true", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            return
        time.sleep(1)
    raise RuntimeError("Debian не ответил по SSH.")


def transport_pids():
    listing = subprocess.check_output(["/bin/ps", "-axww", "-o", "uid=,pid=,comm="],
                                      text=True)
    pids = []
    for line in listing.splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3 and fields[0] == str(os.getuid()) and fields[2] == TRANSPORT:
            pids.append(int(fields[1]))
    return pids


def clean_transport():
    # The call manager daemon can outlive both QEMU and the sudo launcher.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        pids = transport_pids()
        if not pids:
            break
        for pid in pids:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        for _ in range(25):
            if not transport_pids():
                break
            time.sleep(0.2)
    if transport_pids():
        raise RuntimeError("Не удалось закрыть предыдущий сеанс PPTP.")
    for endpoint in RUN.glob(f"*:{SERVER}"):
        if endpoint.is_socket() and endpoint.lstat().st_uid == os.getuid():
            endpoint.unlink(missing_ok=True)


def smb_mounts():
    mounts = subprocess.check_output(["/sbin/mount", "-t", "smbfs"], text=True)
    for line in mounts.splitlines():
        share = re.fullmatch(r"//(?:[^/@]+@)?127\.0\.0\.1:" + str(SMB_PORT)
                             + r"/([^/]+?) on (.+) \(smbfs,.*\)", line, re.IGNORECASE)
        if share and unquote(share[1]).casefold() == PROFILE["share"].casefold():
            yield share[2]


def shutdown(announce=True):
    for path in smb_mounts():
        result = subprocess.run(["/usr/sbin/diskutil", "unmount", path], capture_output=True)
        if result.returncode:
            raise RuntimeError("Закройте открытые файлы на сетевом диске и повторите выключение VPN.")
    was_online = READY.exists()
    READY.unlink(missing_ok=True)
    if running():
        if was_online:
            # Give PPP a chance to send LCP Terminate while its control link is live.
            try:
                guest("sudo systemctl stop ssh-pptp.service", timeout=12, capture_output=True)
            except subprocess.TimeoutExpired:
                pass
        try:
            qmp("system_powerdown")
        except (ConnectionError, FileNotFoundError):
            pass
        for _ in range(45):
            time.sleep(1)
            if not running():
                break
        else:
            raise RuntimeError("Debian пока не завершил работу. Повторите команду выключения.")
    if was_online:
        for _ in range(12):
            if not transport_pids():
                break
            time.sleep(0.5)
    clean_transport()
    if announce:
        print("VPN и виртуальная машина выключены.", flush=True)


def local_address():
    routes = subprocess.check_output(["/usr/sbin/netstat", "-rn", "-f", "inet"], text=True)
    for line in routes.splitlines():
        row = line.split()
        if len(row) >= 4 and row[0] == "default" and re.fullmatch(r"en\d+", row[3]):
            result = subprocess.run(["/usr/sbin/ipconfig", "getifaddr", row[3]],
                                    capture_output=True, text=True)
            if result.returncode == 0:
                return str(ipaddress.IPv4Address(result.stdout.strip()))
    raise RuntimeError("Для PPTP требуется подключение по Wi-Fi или Ethernet.")


def linked():
    hosts = (*HOSTS, SMB_HOST) if SMB_HOST else HOSTS
    result = guest(" && ".join(f"ip -4 route get {host}" for host in hosts),
                   capture_output=True, text=True)
    interfaces = re.findall(r"\bdev (ppp[0-9]+)\b", result.stdout)
    return result.returncode == 0 and len(interfaces) == len(hosts) and len(set(interfaces)) == 1


def forward_smb():
    client = [*SSH[:-1], "-S", SMB_CONTROL]
    if subprocess.run([*client, "-O", "check", "gateway"], capture_output=True).returncode:
        subprocess.run([*client, "-M", "-o", "ExitOnForwardFailure=yes", "-fNT",
                        "-L", f"127.0.0.1:{SMB_PORT}:{SMB_HOST}:445", "gateway"],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True)


def connect_share():
    if not SMB_HOST:
        return
    forward_smb()
    if any(smb_mounts()):
        return
    credential = subprocess.check_output([KEYCHAIN, "--read", "SMB"])
    subprocess.run([str(RESOURCES / "mount-share"), PROFILE["smb_user"], PROFILE["share"]],
                   input=credential, check=True, capture_output=True, timeout=40)
    if not any(smb_mounts()):
        raise RuntimeError("Папка SMB не появилась в Finder.")


class LinkInterrupted(RuntimeError):
    pass


def start_link():
    address = local_address()
    password = subprocess.check_output([KEYCHAIN, "--read", "VPN"]).decode()
    if not password or any(ord(ch) < 32 for ch in password):
        raise ValueError("Нужен непустой пароль VPN.")
    value = password.replace("\\", "\\\\").replace('"', '\\"')
    guest("sudo sh -c 'umask 077; cat > /run/ssh-vpn-secret'",
          input=f'password "{value}"\n'.encode(), check=True)
    del password, value
    with socket.socket(socket.AF_UNIX) as channel, (RUN / "transport.log").open("w+") as log:
        channel.connect(str(RUN / "ppp.sock"))
        process = subprocess.Popen(["/usr/bin/sudo", "-k", "-n", "--", TRANSPORT, SERVER, address],
                                   stdin=channel, stdout=subprocess.DEVNULL, stderr=log,
                                   cwd="/", start_new_session=True)
        time.sleep(0.5)
        if process.poll() is not None:
            log.seek(0)
            raise RuntimeError("Ошибка запуска PPTP: " + log.read(2000))
    guest("sudo systemctl start ssh-pptp.service", check=True)
    for _ in range(50):
        if linked():
            READY.touch(mode=0o600)
            return
        if guest("systemctl is-active --quiet ssh-pptp.service").returncode:
            break
        time.sleep(1)
    status = guest("systemctl show -p ExecMainStatus --value ssh-pptp.service",
                   capture_output=True, text=True, check=True).stdout.strip()
    if status in ("0", "10", "15", "16"):
        raise LinkInterrupted("Сервер PPTP не завершил установку соединения.")
    if status == "19":
        raise RuntimeError("Сервер PPTP отклонил данные для входа.")
    raise RuntimeError(f"Не удалось установить соединение PPTP (код pppd {status}).")


def enable():
    if running() and READY.exists() and linked():
        return
    if running():
        shutdown(announce=False)
    delays = (0, 5, 30)
    for attempt, delay in enumerate(delays):
        time.sleep(delay)
        clean_transport()
        boot()
        try:
            start_link()
            return
        except LinkInterrupted:
            if attempt == len(delays) - 1:
                raise LinkInterrupted("Сервер PPTP не установил соединение после трёх попыток.")
            shutdown(announce=False)


def main():
    os.umask(0o077)
    RUN.mkdir(mode=0o700, exist_ok=True)
    mode = RUN.lstat()
    if not stat.S_ISDIR(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_mode & 0o077:
        raise RuntimeError("Неверный владелец или права временного каталога VPN.")
    with (RUN / "control.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if sys.argv[1:] in (["on"], ["toggle"]):
                print("Другая команда VPN уже выполняется. Проверьте первое окно Терминала.")
                return
            fcntl.flock(lock, fcntl.LOCK_EX)
        command = sys.argv[1:]
        if command == ["toggle"]:
            command = ["off"] if READY.exists() and running() else ["on"]
        if command == ["off"]:
            shutdown()
        elif command == ["on"]:
            try:
                enable()
            except BaseException:
                shutdown(announce=False)
                raise
            try:
                connect_share()
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                raise RuntimeError("SSH VPN включён. Папка SMB пока недоступна. "
                                   "Для повторной попытки запустите pptp-bridge on.") from exc
            print("VPN включён. Папка SMB подключена в Finder." if SMB_HOST else "VPN включён.")
            for host in HOSTS:
                print(f"  ssh {host}")
        else:
            raise ValueError("Используйте on или off.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyboardInterrupt, subprocess.SubprocessError) as exc:
        sys.exit(str(exc) or "Запуск отменён.")
