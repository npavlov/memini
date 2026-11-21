class Memini < Formula
  include Language::Python::Virtualenv

  desc "Query a self-hosted Memini knowledge service"
  homepage "https://github.com/npavlov/memini"
  version "0.1.0rc1"
  license "Apache-2.0"

  on_macos do
    url "https://github.com/npavlov/memini/releases/download/v0.1.0-rc.1/memini-0.1.0rc1-macos-arm64.tar.gz"
    sha256 "a0a868f014e0365d165f69292009aa65579ddea32c046e5ad700d72724a1956b"
    depends_on arch: :arm64
    depends_on macos: :sonoma
  end

  on_linux do
    url "https://github.com/npavlov/memini/releases/download/v0.1.0-rc.1/memini-0.1.0rc1-linux-x86_64.tar.gz"
    sha256 "85daf24478b71d3ca633b5d204c90fdf4daf6ce581f176b504239c575abe89b8"
    depends_on arch: :x86_64
  end

  depends_on "python@3.12"
  deny_network_access! [:build, :postinstall, :test]

  def install
    python = Formula["python@3.12"].opt_bin/"python3.12"
    virtualenv_create(libexec, python.to_s, system_site_packages: false)
    libexec.install "requirements.txt", "wheels"
    bin.write_exec_script opt_libexec/"bin/memini"
  end

  # Wheels carry their own signed, relocatable native libraries. Expand them after keg relocation.
  def post_install
    python = Formula["python@3.12"].opt_bin/"python3.12"
    system python, "-m", "pip", "--isolated", "--python", libexec/"bin/python",
           "install", "--no-index", "--find-links", libexec/"wheels",
           "--require-hashes", "--only-binary=:all:", "-r", libexec/"requirements.txt"
    system python, "-m", "pip", "--isolated", "--python", libexec/"bin/python", "check"
  end

  test do
    assert_equal "memini #{version}", shell_output("#{bin}/memini --version").strip
    assert JSON.parse(shell_output("#{bin}/memini recipe schema")).key?("properties")
  end
end
