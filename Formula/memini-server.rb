class MeminiServer < Formula
  desc "Start and manage a private Memini server with Docker Compose"
  homepage "https://github.com/npavlov/memini"
  url "https://github.com/npavlov/memini/releases/download/v0.2.0-rc.1/memini-server-0.2.0-rc.1-1.zip"
  version "0.2.0-rc.1"
  sha256 "a0345ef19eee86df06d201f9b1d1ce132309e1c33edba9393e081e29ff6d403f"
  license "Apache-2.0"

  depends_on "python@3.12"

  def install
    libexec.install Dir["*"]
    (bin/"memini-server").write <<~SH
      #!/bin/sh
      exec "#{Formula["python@3.12"].opt_bin}/python3.12" "#{opt_libexec}/memini-server.py" "$@"
    SH
  end

  test do
    assert_path_exists Pathname(shell_output("#{bin}/memini-server path").strip)/"docs/examples/mail-thread.json"
  end
end
