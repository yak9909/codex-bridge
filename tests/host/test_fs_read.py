from conftest import make_junction, raises_msg


def test_hidden_is_indistinguishable_from_missing(share):
    e1 = raises_msg(lambda: share.read.read_file("raw/secret/k.txt"))
    e2 = raises_msg(lambda: share.read.read_file("raw/nope.txt"))
    assert e1 == "not found: raw/secret/k.txt" and e2 == "not found: raw/nope.txt"


def test_list_dir_skips_hidden(share):
    names = [e["name"] for e in share.read.list_dir("raw")]
    assert "secret" not in names and "a.txt" in names


def test_list_dir_entry_shape(share):
    e = {x["name"]: x for x in share.read.list_dir("raw")}["a.txt"]
    assert e["type"] == "file" and e["size"] == len("line1\nline2\nthe needle here\nline4\n") and "mtime" in e


def test_list_dir_of_hidden_is_not_found(share):
    assert raises_msg(lambda: share.read.list_dir("raw/secret")) == "not found: raw/secret"


def test_list_dir_skips_junction_to_outside(share):
    make_junction(share.root / "link", share.outside)
    assert "link" not in [e["name"] for e in share.read.list_dir(".")]


def test_find_files_skips_hidden(share):
    found = share.read.find_files("**/*.txt")
    assert "raw/a.txt" in found and all("secret" not in p for p in found)


def test_find_files_relative_to_path(share):
    assert share.read.find_files("*.md", path="docs") == ["docs/readme.md"]


def test_stat(share):
    s = share.read.stat("raw/a.txt")
    assert s["path"] == "raw/a.txt" and s["type"] == "file" and s["size"] > 0


def test_read_small_text(share):
    r = share.read.read_file("raw/a.txt")
    assert r["text"] == "line1\nline2\nthe needle here\nline4\n"
    assert r["encoding"] == "utf-8" and r["truncated"] is False and r["next_offset_line"] is None


def test_read_large_text_is_paged(share):
    r = share.read.read_file("raw/big.txt", max_lines=100)
    assert r["truncated"] and r["next_offset_line"] == 101 and r["text"].count("\n") == 100
    r2 = share.read.read_file("raw/big.txt", offset_line=101, max_lines=1)
    assert r2["text"] == "row 100\n"


def test_read_text_capped_by_bytes(share):
    r = share.read.read_file("raw/big.txt", max_lines=100_000)
    assert r["truncated"] and len(r["text"].encode("utf-8")) <= 256 * 1024
    assert r["next_offset_line"] == r["text"].count("\n") + 1


def test_read_shift_jis(share):
    r = share.read.read_file("raw/sjis.txt")
    assert r["encoding"] == "cp932" and "日本語" in r["text"]


def test_binary_returns_hex_slice(share):
    r = share.read.read_file("raw/data.bin", max_bytes=64)
    assert "hex" in r and len(bytes.fromhex(r["hex"])) == 64 and r["next_byte_offset"] == 64
    r2 = share.read.read_file("raw/data.bin", byte_offset=64, max_bytes=2)
    assert bytes.fromhex(r2["hex"]) == bytes([64, 65])


def test_paused_blocks_everything(share):
    share.set_policy(paused=True)
    assert "停止中" in raises_msg(lambda: share.read.list_dir("."))
