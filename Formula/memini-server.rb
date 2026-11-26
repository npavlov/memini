class MeminiServer < Formula
  desc "Start and manage a private Memini server with Docker Compose"
  homepage "https://github.com/npavlov/memini"
  url "https://github.com/npavlov/memini/releases/download/v0.2.0-rc.5/memini-server-0.2.0-rc.5-1.zip"
  version "0.2.0-rc.5"
  sha256 "3e5ac485cb93510cb0ad222de60a55403be57e8c89409b3656ab1e65f06f953c"
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
