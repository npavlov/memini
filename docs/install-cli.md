# Install the Memini command-line client

Use the CLI to search an existing Memini server from your terminal. Ask its operator for the server URL and a token file with the `read` scope. Save the file somewhere only your account can read. If you also need to run the server, follow the [Docker installation guide](install.md).

## Windows x64

1. Download and run the [v0.1.0-rc.1 Windows x64 installer (.exe)](https://github.com/npavlov/memini/releases/download/v0.1.0-rc.1/memini-0.1.0-rc.1-windows-x64.exe). It bundles Python and installs for your Windows account under `%LOCALAPPDATA%\Programs\Memini`.
2. Open a **new PowerShell window** so it picks up the updated user PATH. Run `memini --version`. For this release, expect `memini 0.1.0rc1`.
3. Set the server address and the path where you saved the token. These example values apply to the current PowerShell session:

```powershell
$env:MEMINI_URL = 'https://memini.example.org'
$env:MEMINI_TOKEN_FILE = Join-Path $HOME '.config\memini\read.token'
memini workspace list
```

If PowerShell cannot find the command after reopening, run `& "$env:LOCALAPPDATA\Programs\Memini\bin\memini.exe" --version` to check the installation directly.

**Upgrade** by running the newer Windows x64 installer from [Releases](https://github.com/npavlov/memini/releases). **Uninstall** by opening Settings → Apps → Installed apps, finding **Memini CLI** and choosing **Uninstall**.

## Homebrew on macOS and Linux

The formula supports **Apple Silicon Macs running macOS Sonoma (14) or later** and **x86_64 Linux** with [Homebrew](https://brew.sh/) installed. Check `uname -m` for `arm64` on macOS or `x86_64` on Linux. Homebrew supplies Python 3.12 and installs the CLI in a private environment.

```sh
brew tap npavlov/memini https://github.com/npavlov/memini.git
brew install npavlov/memini/memini
memini --version
```

For v0.1.0-rc.1, expect `memini 0.1.0rc1`. If the installation succeeded but the command is missing, check `"$(brew --prefix)/bin/memini" --version` and apply the shell setup printed by Homebrew.

In Bash or Zsh, replace the example URL and file path with the values from your operator. These settings apply to the current shell:

```sh
export MEMINI_URL='https://memini.example.org'
export MEMINI_TOKEN_FILE="$HOME/.config/memini/read.token"
memini workspace list
```

To **upgrade** after a new formula is published:

```sh
brew update
brew upgrade npavlov/memini/memini
```

To **uninstall** the client and remove its tap:

```sh
brew uninstall npavlov/memini/memini
brew untap npavlov/memini
```

## Check your connection

`memini workspace list` shows the workspaces your token can read, or reports that there are none yet. “Cannot read” usually means the token file path is wrong. For a refused token (exit 5), ask the operator to check its expiry, workspace and scope. For a connection failure (exit 3), check the server URL and any required VPN.

Once you see a workspace, use `memini search "archive retention" --workspace demo --limit 3`, replacing `demo` with its name. Each result identifies the source of the matching text. [CLI reference](cli.md)
