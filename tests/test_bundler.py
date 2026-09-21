import zipfile

from conftest import make_files

from uarb_agent.bundler import archive_member_name, bundle, safe_name


def test_safe_name_strips_path_characters():
    assert safe_name('a/b\\c:d*e?"f<g>h|i.pdf') == "a_b_c_d_e_f_g_h_i.pdf"
    assert safe_name("   ") == "file"


def test_member_name_uses_doc_id_and_title(tmp_path):
    f = make_files(tmp_path, [10])[0]
    assert archive_member_name(f) == "100000 - Document 0_ draft_final_.pdf"


def test_single_zip_when_under_budget(tmp_path):
    files = make_files(tmp_path, [1000, 2000, 3000])
    b = bundle(files, tmp_path / "out", "M12205 Other Documents", budget_bytes=10_000_000)
    assert len(b.parts) == 1
    assert b.parts[0].path.name == "M12205 Other Documents.zip"
    with zipfile.ZipFile(b.parts[0].path) as z:
        assert len(z.namelist()) == 3
    assert not b.too_large


def test_splits_into_parts_by_budget(tmp_path):
    files = make_files(tmp_path, [600_000, 600_000, 600_000, 600_000])
    b = bundle(files, tmp_path / "out", "M1", budget_bytes=1_300_000)
    assert [len(p.members) for p in b.parts] == [2, 2]
    assert b.parts[0].path.name == "M1 part 1 of 2.zip"
    assert all(p.size <= 1_300_000 for p in b.parts)


def test_oversized_file_reported_not_sent(tmp_path):
    files = make_files(tmp_path, [100, 5_000_000, 100])
    b = bundle(files, tmp_path / "out", "M1", budget_bytes=1_000_000)
    assert [f.row.doc_id for f in b.too_large] == ["100001"]
    assert len(b.files) == 2


def test_no_files_no_parts(tmp_path):
    b = bundle([], tmp_path / "out", "M1", budget_bytes=100)
    assert b.parts == []


def test_duplicate_member_names_disambiguated(tmp_path):
    files = make_files(tmp_path, [10, 10])
    files[1].row.doc_id = files[0].row.doc_id
    files[1].row.title = files[0].row.title
    b = bundle(files, tmp_path / "out", "M1")
    with zipfile.ZipFile(b.parts[0].path) as z:
        assert len(set(z.namelist())) == 2
