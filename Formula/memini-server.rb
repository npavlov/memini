class MeminiServer < Formula
  desc "Start and manage a private Memini server with Docker Compose"
  homepage "https://github.com/npavlov/memini"
  url "https://github.com/npavlov/memini/releases/download/v0.2.0-rc.1/memini-server-0.2.0-rc.1-4.zip"
  version "0.2.0-rc.1"
  sha256 "452a7bcedc36e891c57b6358fec3da5eaba228fe599a2ca9baa43cf45bb756a5"
  revision 3
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
