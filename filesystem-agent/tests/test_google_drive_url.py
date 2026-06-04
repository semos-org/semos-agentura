"""Unit tests for Google Drive URL parsing (no network, no credentials)."""

from filesystem_agent._google_drive import (
    is_google_drive_url,
    resolve_google_drive_url,
)


class TestIsGoogleDriveUrl:
    def test_drive_folder(self):
        assert is_google_drive_url("https://drive.google.com/drive/folders/1ABC123?usp=sharing")

    def test_drive_file(self):
        assert is_google_drive_url("https://drive.google.com/file/d/1XYZ789/view?usp=sharing")

    def test_google_doc(self):
        assert is_google_drive_url("https://docs.google.com/document/d/1DOC456/edit?usp=sharing")

    def test_google_sheet(self):
        assert is_google_drive_url("https://docs.google.com/spreadsheets/d/1SHEET/edit")

    def test_google_slides(self):
        assert is_google_drive_url("https://docs.google.com/presentation/d/1SLIDES/edit")

    def test_not_google(self):
        assert not is_google_drive_url("https://contoso.sharepoint.com/sites/X")

    def test_not_google_random(self):
        assert not is_google_drive_url("https://example.com/drive/folders/abc")


class TestResolveGoogleDriveUrl:
    def test_folder_link(self):
        url = "https://drive.google.com/drive/folders/1ABC-def_GHI?usp=sharing"
        target = resolve_google_drive_url(url)
        assert target.item_id == "1ABC-def_GHI"
        assert target.item_type == "folder"
        assert not target.is_google_doc

    def test_folder_link_with_account_selector(self):
        # /drive/u/0/folders/ is the multi-account URL format
        url = "https://drive.google.com/drive/u/0/folders/1ABC_def-GHI?ths=true"
        target = resolve_google_drive_url(url)
        assert target.item_id == "1ABC_def-GHI"
        assert target.item_type == "folder"

    def test_folder_link_no_query(self):
        url = "https://drive.google.com/drive/folders/1ABC123"
        target = resolve_google_drive_url(url)
        assert target.item_id == "1ABC123"
        assert target.item_type == "folder"

    def test_file_link(self):
        url = "https://drive.google.com/file/d/1XYZ-789_abc/view?usp=sharing"
        target = resolve_google_drive_url(url)
        assert target.item_id == "1XYZ-789_abc"
        assert target.item_type == "file"
        assert not target.is_google_doc

    def test_google_doc(self):
        url = "https://docs.google.com/document/d/1DOC456/edit?usp=sharing"
        target = resolve_google_drive_url(url)
        assert target.item_id == "1DOC456"
        assert target.item_type == "document"
        assert target.is_google_doc

    def test_google_sheet(self):
        url = "https://docs.google.com/spreadsheets/d/1SHEET789/edit#gid=0"
        target = resolve_google_drive_url(url)
        assert target.item_id == "1SHEET789"
        assert target.item_type == "spreadsheet"
        assert target.is_google_doc

    def test_google_slides(self):
        url = "https://docs.google.com/presentation/d/1SLIDES000/edit?usp=sharing"
        target = resolve_google_drive_url(url)
        assert target.item_id == "1SLIDES000"
        assert target.item_type == "presentation"
        assert target.is_google_doc

    def test_my_drive(self):
        url = "https://drive.google.com/drive/my-drive"
        target = resolve_google_drive_url(url)
        assert target.item_id == "root"
        assert target.item_type == "folder"

    def test_my_drive_with_account(self):
        url = "https://drive.google.com/drive/u/0/my-drive"
        target = resolve_google_drive_url(url)
        assert target.item_id == "root"
        assert target.item_type == "folder"

    def test_shared_with_me(self):
        url = "https://drive.google.com/drive/shared-with-me"
        target = resolve_google_drive_url(url)
        assert target.item_id == "sharedWithMe"
        assert target.item_type == "shared"

    def test_unknown_url(self):
        url = "https://example.com/not-a-drive-link"
        target = resolve_google_drive_url(url)
        assert target.item_id == ""
        assert target.item_type == ""
        assert not target.is_google_doc

    def test_drive_open_link(self):
        # Alternative format: /open?id=FILE_ID - not supported yet
        url = "https://drive.google.com/open?id=1ABC123"
        target = resolve_google_drive_url(url)
        assert target.item_id == ""  # Not matched by current patterns

    def test_long_id(self):
        long_id = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
        url = f"https://docs.google.com/document/d/{long_id}/edit"
        target = resolve_google_drive_url(url)
        assert target.item_id == long_id
        assert target.item_type == "document"
