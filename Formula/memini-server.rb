class MeminiServer < Formula
  desc "Start and manage a private Memini server with Docker Compose"
  homepage "https://github.com/npavlov/memini"
  url "https://github.com/npavlov/memini/releases/download/v0.1.0/memini-server-0.1.0-3.zip"
  version "0.1.0"
  sha256 "04815a23fdacfe717d63eb00230be0a0aa04430ed638044547303e20eb62daf5"
  revision 2
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
