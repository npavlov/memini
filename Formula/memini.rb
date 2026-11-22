class Memini < Formula
  include Language::Python::Virtualenv

  desc "Query a self-hosted Memini knowledge service"
  homepage "https://github.com/npavlov/memini"
  version "0.1.0"
  license "Apache-2.0"

  on_macos do
    url "https://github.com/npavlov/memini/releases/download/v0.1.0/memini-0.1.0-macos-arm64.tar.gz"
    sha256 "75da160c928559111bda2ec8825d20b6a86e67fadc3f98182134921c2acf7b02"
    depends_on arch: :arm64
    depends_on macos: :sonoma
  end

  on_linux do
    url "https://github.com/npavlov/memini/releases/download/v0.1.0/memini-0.1.0-linux-x86_64.tar.gz"
    sha256 "be8c17a7e9216b612363633a9e220e132f92c7454ac3e0536dad3c6f01eb40d8"
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
