
#!/usr/bin/env python3

import os
import re
import sys
import time
import shutil
import socket
import getpass
import subprocess
import urllib.request
from pathlib import Path

# ============================================================
# SPARK AGENT CONFIGURATION
# ============================================================

SPARK_VERSION = "3.5.0"
SPARK_ARCHIVE = f"spark-{SPARK_VERSION}-bin-hadoop3.tgz"
SPARK_URL = (
    f"https://archive.apache.org/dist/spark/spark-{SPARK_VERSION}/"
    f"{SPARK_ARCHIVE}"
)

HOME = Path.home()
SPARK_HOME = HOME / "spark"
AGENT_HOME = HOME / ".spark-agent"
DOWNLOAD_FILE = AGENT_HOME / SPARK_ARCHIVE

MASTER_PORT = 7077
MASTER_WEBUI_PORT = 8080
WORKER_WEBUI_PORT = 8081


# ============================================================
# GENERAL HELPERS
# ============================================================

def run_command(command, timeout=30):
    """Run a local command and return its result."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "Command timed out."
    except FileNotFoundError:
        return 127, "", f"Command not found: {command[0]}"


def ask_yes_no(message):
    while True:
        answer = input(f"{message} [y/n]: ").strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please enter y or n.")


def get_local_ip():
    """Find the IP used for the default network route."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"


def command_exists(name):
    return shutil.which(name) is not None


def get_java_version():
    code, output, error = run_command(["java", "-version"])
    return code == 0, output or error


# ============================================================
# STARTUP PREREQUISITE CHECKS
# IMPORTANT: THIS FUNCTION NEVER DOWNLOADS SPARK.
# ============================================================

def ensure_local_prerequisites():
    print("\n" + "=" * 60)
    print("STARTUP CHECK: AGENT REQUIREMENTS")
    print("=" * 60)

    # Python is required to run this program.
    print(f"[OK] Python {sys.version.split()[0]}")

    missing_packages = []

    for command, package in [
        ("curl", "curl"),
        ("tar", "tar"),
    ]:
        if command_exists(command):
            print(f"[OK] {command} is available.")
        else:
            print(f"[MISSING] {command}")
            missing_packages.append(package)

    java_ok, java_info = get_java_version()

    if java_ok:
        print("[OK] Java is installed.")
        print(java_info.splitlines()[0] if java_info else "")
    else:
        print("[MISSING] Java runtime")
        missing_packages.append("openjdk-11-jdk")

    # Install only missing OS prerequisites after permission.
    if missing_packages:
        packages = sorted(set(missing_packages))

        print("\nMissing prerequisite packages:")
        for package in packages:
            print(f"  - {package}")

        if not ask_yes_no("Install these prerequisites using apt?"):
            print(
                "\nRequired prerequisites are missing. "
                "The agent cannot continue."
            )
            return False

        if not command_exists("apt-get"):
            print("Automatic installation is supported only with apt-get.")
            print("Please install the missing packages manually.")
            return False

        print("\nInstalling prerequisites...")
        command = [
            "sudo", "apt-get", "install", "-y",
            *packages
        ]

        try:
            result = subprocess.run(command)
        except KeyboardInterrupt:
            print("\nPrerequisite installation cancelled.")
            return False

        if result.returncode != 0:
            print("Prerequisite installation failed.")
            return False

        # Verify the requirements again.
        still_missing = [
            package
            for command_name, package in [
                ("curl", "curl"),
                ("tar", "tar"),
            ]
            if not command_exists(command_name)
        ]

        java_ok, _ = get_java_version()

        if still_missing or not java_ok:
            print("Some prerequisites are still missing.")
            return False

    # Check Paramiko without installing it automatically.
    try:
        import paramiko  # noqa: F401
        print("[OK] Python SSH library (Paramiko) is available.")
    except ImportError:
        print("[INFO] Paramiko is not installed.")
        print("Remote-node management needs Paramiko.")
        print("Local mode does not need Paramiko.")

    print("\nPrerequisite check completed.")
    return True


# ============================================================
# PORT CHECKING
# ============================================================

def port_is_available(port, host="0.0.0.0"):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def display_ports():
    print("\nPort availability:")

    for port, description in [
        (MASTER_PORT, "Spark Master"),
        (MASTER_WEBUI_PORT, "Spark Master Web UI"),
        (WORKER_WEBUI_PORT, "Spark Worker Web UI"),
    ]:
        status = (
            "AVAILABLE"
            if port_is_available(port)
            else "IN USE"
        )
        print(f"  {port:<6} {description:<25} {status}")


def choose_port(default_port, description):
    if port_is_available(default_port):
        return default_port

    print(f"\nDefault port {default_port} for {description} is in use.")

    while True:
        value = input(
            f"Enter an available port for {description}: "
        ).strip()

        try:
            port = int(value)
            if not 1024 <= port <= 65535:
                print("Choose a port between 1024 and 65535.")
                continue

            if port_is_available(port):
                return port

            print("That port is already in use.")
        except ValueError:
            print("Enter a valid number.")


# ============================================================
# SPARK DOWNLOAD AND INSTALL
# Called ONLY from Option 2.
# ============================================================

def download_spark():
    AGENT_HOME.mkdir(parents=True, exist_ok=True)

    if SPARK_HOME.exists():
        print(f"Spark directory already exists: {SPARK_HOME}")
        return True

    if DOWNLOAD_FILE.exists():
        print(
            f"Partial download found: {DOWNLOAD_FILE}\n"
            "The download will try to resume."
        )

    print(f"\nSpark version: {SPARK_VERSION}")
    print(f"Download URL: {SPARK_URL}")
    print("The download may take a long time on a slow connection.")

    if not ask_yes_no("Continue with Spark download and installation?"):
        print("Spark installation cancelled.")
        return False

    # curl -C - resumes an existing partial file.
    command = [
        "curl",
        "-fL",
        "--retry", "3",
        "--retry-delay", "3",
        "-C", "-",
        "-o", str(DOWNLOAD_FILE),
        SPARK_URL,
    ]

    try:
        result = subprocess.run(command)
    except KeyboardInterrupt:
        print("\nDownload interrupted.")
        print("Run Install / Configure again to retry the download.")
        return False

    if result.returncode != 0:
        print("Spark download failed or was interrupted.")
        print("The partial file is retained when possible.")
        return False

    print("\nExtracting Spark archive...")

    code, _, error = run_command(
        ["tar", "-xzf", str(DOWNLOAD_FILE), "-C", str(AGENT_HOME)],
        timeout=600
    )

    if code != 0:
        print(f"Extraction failed: {error}")
        return False

    extracted_dir = AGENT_HOME / f"spark-{SPARK_VERSION}-bin-hadoop3"

    if not extracted_dir.is_dir():
        print("Expected Spark directory was not found after extraction.")
        return False

    try:
        if SPARK_HOME.exists():
            shutil.rmtree(SPARK_HOME)
        extracted_dir.rename(SPARK_HOME)
    except OSError as error:
        print(f"Could not prepare Spark directory: {error}")
        return False

    print(f"[OK] Spark installed at {SPARK_HOME}")
    return True


# ============================================================
# SPARK STATUS AND CONFIGURATION
# ============================================================

def spark_processes():
    code, output, _ = run_command(["jps", "-l"])

    if code != 0:
        return []

    return output.splitlines()


def process_running(process_name):
    return any(
        process_name in line
        for line in spark_processes()
    )


def write_spark_environment(master_host, master_port, worker_webui_port):
    conf_dir = SPARK_HOME / "conf"
    conf_dir.mkdir(parents=True, exist_ok=True)

    env_file = conf_dir / "spark-env.sh"

    content = f"""#!/usr/bin/env bash
export JAVA_HOME="${{JAVA_HOME:-$(dirname $(dirname $(readlink -f $(which java))))}}"
export SPARK_MASTER_HOST="{master_host}"
export SPARK_MASTER_PORT="{master_port}"
export SPARK_MASTER_WEBUI_PORT="{MASTER_WEBUI_PORT}"
export SPARK_WORKER_WEBUI_PORT="{worker_webui_port}"
"""

    env_file.write_text(content)
    env_file.chmod(0o755)

    workers_file = conf_dir / "workers"
    workers_file.write_text("localhost\n")


def configure_local_spark():
    if not SPARK_HOME.is_dir():
        print("Spark is not installed. Choose Option 2 first.")
        return False

    display_ports()

    master_port = choose_port(MASTER_PORT, "Spark Master")
    master_ui_port = choose_port(
        MASTER_WEBUI_PORT, "Spark Master Web UI"
    )
    worker_ui_port = choose_port(
        WORKER_WEBUI_PORT, "Spark Worker Web UI"
    )

    ip = get_local_ip()

    # Keep the selected master port and UI port in the environment.
    conf_dir = SPARK_HOME / "conf"
    conf_dir.mkdir(parents=True, exist_ok=True)

    env_file = conf_dir / "spark-env.sh"
    content = f"""#!/usr/bin/env bash
export JAVA_HOME="${{JAVA_HOME:-$(dirname $(dirname $(readlink -f $(which java))))}}"
export SPARK_MASTER_HOST="{ip}"
export SPARK_MASTER_PORT="{master_port}"
export SPARK_MASTER_WEBUI_PORT="{master_ui_port}"
export SPARK_WORKER_WEBUI_PORT="{worker_ui_port}"
"""
    env_file.write_text(content)
    env_file.chmod(0o755)

    print("\nLocal Spark configuration saved.")

    start = SPARK_HOME / "sbin" / "start-master.sh"
    stop = SPARK_HOME / "sbin" / "stop-master.sh"
    worker_start = SPARK_HOME / "sbin" / "start-worker.sh"

    master_running = process_running("org.apache.spark.deploy.master.Master")

    if master_running:
        print("[OK] Spark Master is already running. Not starting it again.")
    else:
        code, output, error = run_command(
            [str(start), "--host", ip, "--port", str(master_port)],
            timeout=60
        )

        if code != 0:
            print(f"Could not start Spark Master: {error or output}")
            return False

    time.sleep(2)

    worker_running = process_running("org.apache.spark.deploy.worker.Worker")

    if worker_running:
        print("[OK] Spark Worker is already running.")
    else:
        master_url = f"spark://{ip}:{master_port}"
        code, output, error = run_command(
            [str(worker_start), master_url],
            timeout=60
        )

        if code != 0:
            print(f"Could not start Spark Worker: {error or output}")
            return False

    print("\n[OK] Local Spark configuration completed.")
    print(f"Master URL: spark://{ip}:{master_port}")
    print(f"Master Web UI: http://{ip}:{master_ui_port}")
    print(f"Worker Web UI: http://{ip}:{worker_ui_port}")

    return True


# ============================================================
# SSH SUPPORT FOR REMOTE MASTER AND WORKERS
# ============================================================

class RemoteNode:
    def __init__(self, name, host, username, password):
        self.name = name
        self.host = host
        self.username = username
        self.password = password
        self.client = None

    def connect(self):
        try:
            import paramiko
        except ImportError:
            print(
                "Paramiko is required for remote management.\n"
                "Install it with: sudo apt-get install python3-paramiko"
            )
            return False

        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(
                paramiko.AutoAddPolicy()
            )
            client.connect(
                hostname=self.host,
                username=self.username,
                password=self.password,
                timeout=15,
                banner_timeout=15,
                auth_timeout=15,
            )
            self.client = client
            return True
        except Exception as error:
            print(f"SSH connection to {self.name} failed: {error}")
            return False

    def execute(self, command, timeout=60):
        if self.client is None:
            return 1, "", "SSH is not connected."

        try:
            _, stdout, stderr = self.client.exec_command(
                command,
                timeout=timeout
            )
            exit_code = stdout.channel.recv_exit_status()
            return (
                exit_code,
                stdout.read().decode(errors="replace").strip(),
                stderr.read().decode(errors="replace").strip(),
            )
        except Exception as error:
            return 1, "", str(error)

    def close(self):
        if self.client:
            self.client.close()
            self.client = None


def prompt_remote_node(name):
    print(f"\n{name} connection details")

    host = input("IP address or hostname: ").strip()
    username = input("SSH username: ").strip()
    password = getpass.getpass("SSH password: ")

    if not host or not username:
        print("Host and username are required.")
        return None

    node = RemoteNode(name, host, username, password)

    if not node.connect():
        return None

    print(f"[OK] Connected to {name} ({host})")
    return node


def install_remote_spark(node):
    """Download and extract Spark on a remote host after confirmation."""
    print(f"\nSpark will be installed on {node.name}: {node.host}")

    if not ask_yes_no(f"Install Spark on {node.name}?"):
        return False

    home_code, home, error = node.execute("printf '%s' \"$HOME\"")

    if home_code != 0 or not home:
        print(f"Could not determine remote home directory: {error}")
        return False

    remote_spark = f"{home}/spark"
    remote_cache = f"{home}/.spark-agent"
    remote_archive = f"{remote_cache}/{SPARK_ARCHIVE}"

    code, output, _ = node.execute(
        f"test -d {remote_spark} && echo EXISTS || echo MISSING"
    )

    if "EXISTS" in output:
        print(f"Spark already exists on {node.name}: {remote_spark}")
        return True

    node.execute(f"mkdir -p {remote_cache}")

    download_command = (
        f"curl -fL --retry 3 --retry-delay 3 -C - "
        f"-o '{remote_archive}' '{SPARK_URL}'"
    )

    print(f"Downloading Spark on {node.name}. This may take time.")
    code, output, error = node.execute(download_command, timeout=3600)

    if code != 0:
        print(f"Remote download failed: {error or output}")
        return False

    code, output, error = node.execute(
        f"tar -xzf '{remote_archive}' -C '{remote_cache}' "
        f"&& mv '{remote_cache}/spark-{SPARK_VERSION}-bin-hadoop3' "
        f"'{remote_spark}'",
        timeout=600
    )

    if code != 0:
        print(f"Remote extraction failed: {error or output}")
        return False

    print(f"[OK] Spark installed on {node.name}.")
    return True


def configure_remote_node(node, master_url=None, is_master=False):
    home_code, home, error = node.execute("printf '%s' \"$HOME\"")

    if home_code != 0 or not home:
        print(f"Could not find remote home: {error}")
        return False

    spark_home = f"{home}/spark"

    code, _, error = node.execute(f"test -d '{spark_home}'")

    if code != 0:
        print(f"Spark is not installed on {node.name}.")
        return False

    if is_master:
        host = node.host
        master_port = choose_remote_port(node, MASTER_PORT, "Master")
        ui_port = choose_remote_port(
            node, MASTER_WEBUI_PORT, "Master Web UI"
        )

        command = f"""cat > '{spark_home}/conf/spark-env.sh' <<'EOF'
#!/usr/bin/env bash
export SPARK_MASTER_HOST="{host}"
export SPARK_MASTER_PORT="{master_port}"
export SPARK_MASTER_WEBUI_PORT="{ui_port}"
EOF
chmod +x '{spark_home}/conf/spark-env.sh'
"""
        code, _, error = node.execute(command)

        if code != 0:
            print(f"Master configuration failed: {error}")
            return False

        code, output, error = node.execute(
            "jps -l | grep -q "
            "'org.apache.spark.deploy.master.Master' && echo RUNNING || echo STOPPED"
        )

        if "RUNNING" not in output:
            code, output, error = node.execute(
                f"'{spark_home}/sbin/start-master.sh' "
                f"--host '{host}' --port {master_port}",
                timeout=60
            )

            if code != 0:
                print(f"Master start failed: {error or output}")
                return False
        else:
            print("[OK] Remote Master is already running.")

        node.master_url = f"spark://{host}:{master_port}"
        node.master_ui = f"http://{host}:{ui_port}"

        print(f"Master URL: {node.master_url}")
        print(f"Master Web UI: {node.master_ui}")
        return True

    if not master_url:
        print("Master URL is required for a worker.")
        return False

    worker_ui = choose_remote_port(node, WORKER_WEBUI_PORT, "Worker Web UI")

    command = f"""cat > '{spark_home}/conf/spark-env.sh' <<'EOF'
#!/usr/bin/env bash
export SPARK_WORKER_WEBUI_PORT="{worker_ui}"
EOF
chmod +x '{spark_home}/conf/spark-env.sh'
"""

    code, _, error = node.execute(command)

    if code != 0:
        print(f"Worker configuration failed: {error}")
        return False

    code, output, _ = node.execute(
        "jps -l | grep -q "
        "'org.apache.spark.deploy.worker.Worker' && echo RUNNING || echo STOPPED"
    )

    if "RUNNING" in output:
        print("[OK] Remote Worker is already running.")
        return True

    code, output, error = node.execute(
        f"'{spark_home}/sbin/start-worker.sh' '{master_url}'",
        timeout=60
    )

    if code != 0:
        print(f"Worker start failed: {error or output}")
        return False

    print(f"[OK] Worker configured on {node.name}.")
    print(f"Worker Web UI: http://{node.host}:{worker_ui}")
    return True


def choose_remote_port(node, default_port, description):
    code, output, _ = node.execute(
        f"python3 -c \"import socket; "
        f"s=socket.socket(); "
        f"exec('try:\\\\n s.bind((\\'0.0.0.0\\',{default_port})); "
        f"print(\\'AVAILABLE\\')\\\\nexcept OSError: "
        f"print(\\'IN_USE\\')\\\\nfinally: s.close()')\""
    )

    if "AVAILABLE" in output:
        print(f"{description} port {default_port}: AVAILABLE")
        return default_port

    print(f"{description} port {default_port}: IN USE")

    while True:
        try:
            port = int(input(f"Enter another port for {description}: "))
            if 1024 <= port <= 65535:
                check_code, check_output, _ = node.execute(
                    f"ss -ltn | grep -q ':{port} ' "
                    f"&& echo IN_USE || echo AVAILABLE"
                )
                if "AVAILABLE" in check_output:
                    return port
                print("Port is in use.")
            else:
                print("Enter a port between 1024 and 65535.")
        except ValueError:
            print("Enter a valid port number.")


# ============================================================
# VERIFY
# ============================================================

def verify_local():
    print("\nLOCAL SPARK VERIFICATION")
    print("-" * 40)

    if SPARK_HOME.is_dir():
        print(f"[INSTALLED] {SPARK_HOME}")
        version_script = SPARK_HOME / "bin" / "spark-submit"

        code, output, error = run_command(
            [str(version_script), "--version"],
            timeout=30
        )

        print(output or error or "Spark version command completed.")
    else:
        print("[NOT INSTALLED] Spark directory not found.")

    processes = spark_processes()

    master = any(
        "org.apache.spark.deploy.master.Master" in line
        for line in processes
    )
    worker = any(
        "org.apache.spark.deploy.worker.Worker" in line
        for line in processes
    )

    print(f"Master status: {'RUNNING' if master else 'STOPPED'}")
    print(f"Worker status: {'RUNNING' if worker else 'STOPPED'}")

    if master:
        ip = get_local_ip()
        print(f"Master address: spark://{ip}:{MASTER_PORT}")
        print(f"Master Web UI: http://{ip}:{MASTER_WEBUI_PORT}")


def verify_remote(node):
    print(f"\nVERIFYING {node.name}: {node.host}")

    code, output, _ = node.execute(
        'test -d "$HOME/spark" && echo INSTALLED || echo NOT_INSTALLED'
    )

    print(f"Installation: {output or 'UNKNOWN'}")

    code, output, _ = node.execute("jps -l")

    if code == 0:
        print(output or "No Java processes found.")
    else:
        print("Could not retrieve Java process status.")


# ============================================================
# INSTALL / CONFIGURE MENU ACTION
# ============================================================

def install_action():
    print("\nINSTALL / CONFIGURE SPARK")
    print("-" * 40)

    try:
        workers = int(input("Number of worker nodes (0 for local): ").strip())
        if workers < 0:
            print("Worker count cannot be negative.")
            return
    except ValueError:
        print("Enter a valid worker count.")
        return

    display_ports()

    if workers == 0:
        # No SSH connection for local configuration.
        if not download_spark():
            return

        configure_local_spark()
        return

    master = prompt_remote_node("Master")

    if master is None:
        return

    worker_nodes = []

    try:
        if not install_remote_spark(master):
            return

        if not configure_remote_node(master, is_master=True):
            return

        master_url = getattr(master, "master_url", None)

        if not master_url:
            print("Could not determine the Master URL.")
            return

        for index in range(workers):
            worker = prompt_remote_node(f"Worker {index + 1}")

            if worker is None:
                print("Could not connect to a worker. Stopping setup.")
                return

            worker_nodes.append(worker)

            if not install_remote_spark(worker):
                return

            if not configure_remote_node(
                worker, master_url=master_url, is_master=False
            ):
                return

        print("\n[OK] Remote Spark configuration completed.")
        print(f"Master URL: {master_url}")

    finally:
        master.close()

        for worker in worker_nodes:
            worker.close()


# ============================================================
# DELETE
# ============================================================

def delete_local():
    print("\nDELETE LOCAL SPARK")
    print("-" * 40)

    if not SPARK_HOME.exists():
        print("Local Spark installation directory does not exist.")
        return

    if not ask_yes_no(f"Delete {SPARK_HOME}?"):
        print("Deletion cancelled.")
        return

    # Stop Spark daemons before removing the directory.
    stop_master = SPARK_HOME / "sbin" / "stop-master.sh"
    stop_worker = SPARK_HOME / "sbin" / "stop-worker.sh"

    if stop_worker.exists():
        run_command([str(stop_worker)], timeout=30)

    if stop_master.exists():
        run_command([str(stop_master)], timeout=30)

    try:
        shutil.rmtree(SPARK_HOME)
        print("[OK] Spark installation directory deleted.")
    except OSError as error:
        print(f"Deletion failed: {error}")


def delete_remote(node):
    home_code, home, error = node.execute("printf '%s' \"$HOME\"")

    if home_code != 0 or not home:
        print(f"Could not determine remote home: {error}")
        return

    spark_home = f"{home}/spark"

    code, output, _ = node.execute(
        f"test -d '{spark_home}' && echo EXISTS || echo MISSING"
    )

    if "MISSING" in output:
        print(f"Spark is not installed on {node.name}.")
        return

    if not ask_yes_no(
        f"Delete Spark directory on {node.name} ({spark_home})?"
    ):
        print("Deletion cancelled.")
        return

    node.execute(f"'{spark_home}/sbin/stop-worker.sh' 2>/dev/null || true")
    node.execute(f"'{spark_home}/sbin/stop-master.sh' 2>/dev/null || true")

    code, output, error = node.execute(
        f"rm -rf -- '{spark_home}'"
    )

    if code == 0:
        print(f"[OK] Spark directory deleted on {node.name}.")
    else:
        print(f"Deletion failed: {error or output}")


def delete_action():
    print("\nDELETE SPARK")
    print("1. Delete local Spark")
    print("2. Delete remote Spark")
    print("3. Cancel")

    choice = input("Choose an option: ").strip()

    if choice == "1":
        delete_local()

    elif choice == "2":
        node = prompt_remote_node("Remote machine")

        if node:
            try:
                delete_remote(node)
            finally:
                node.close()

    elif choice == "3":
        print("Deletion cancelled.")

    else:
        print("Invalid option.")


# ============================================================
# VERIFY MENU ACTION
# ============================================================

def verify_action():
    print("\nVERIFY SPARK")
    print("1. Verify local machine")
    print("2. Verify remote machine")
    print("3. Back")

    choice = input("Choose an option: ").strip()

    if choice == "1":
        verify_local()

    elif choice == "2":
        node = prompt_remote_node("Remote machine")

        if node:
            try:
                verify_remote(node)
            finally:
                node.close()

    elif choice == "3":
        return

    else:
        print("Invalid option.")


# ============================================================
# MAIN MENU
# ============================================================

def show_menu():
    print("\n" + "=" * 50)
    print("             SPARK AGENT")
    print("=" * 50)
    print("1. Verify")
    print("2. Install / Configure")
    print("3. Delete")
    print("4. Exit")
    print("=" * 50)


def main():
    # Startup checks only. Never install or download Spark here.
    if not ensure_local_prerequisites():
        sys.exit(1)

    while True:
        show_menu()
        choice = input("Enter your choice: ").strip()

        try:
            if choice == "1":
                verify_action()

            elif choice == "2":
                install_action()

            elif choice == "3":
                delete_action()

            elif choice == "4":
                print("Spark Agent closed.")
                break

            else:
                print("Invalid choice. Enter 1, 2, 3, or 4.")

        except KeyboardInterrupt:
            print("\nOperation cancelled by user.")

        except Exception as error:
            print(f"\nUnexpected error: {error}")

        input("\nPress Enter to return to the main menu...")


if __name__ == "__main__":
    main()
