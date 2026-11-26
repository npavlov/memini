class MeminiServer < Formula
  desc "Start and manage a private Memini server with Docker Compose"
  homepage "https://github.com/npavlov/memini"
  url "https://github.com/npavlov/memini/releases/download/v0.2.0-rc.4/memini-server-0.2.0-rc.4-1.zip"
  version "0.2.0-rc.4"
  sha256 "251e26a6b526a45afe80dc60e08cc42b00c1bc4fb0db4b0c82b1009693bd93ed"
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
