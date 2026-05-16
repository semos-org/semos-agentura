"""Unit tests for SharePoint URL resolution and doc library detection."""

from __future__ import annotations

from filesystem_agent._sharepoint import (
    detect_doc_library,
    extract_shared_folder,
    resolve_sharepoint_url,
)


class TestResolveSharepointUrl:
    resolve = staticmethod(resolve_sharepoint_url)

    def test_team_site(self):
        url = "https://contoso.sharepoint.com/sites/MySite"
        assert self.resolve(url) == (url, "")

    def test_team_site_trailing_slash(self):
        site, sub = self.resolve("https://contoso.sharepoint.com/sites/MySite/")
        assert site == "https://contoso.sharepoint.com/sites/MySite"
        assert sub == ""

    def test_team_site_with_doc_library(self):
        site, sub = self.resolve("https://contoso.sharepoint.com/sites/MySite/Shared%20Documents/General")
        assert site == "https://contoso.sharepoint.com/sites/MySite"

    def test_personal_site(self):
        url = "https://tenant-my.sharepoint.com/personal/user_domain_com"
        assert self.resolve(url) == (url, "")

    def test_personal_site_with_path(self):
        site, sub = self.resolve("https://tenant-my.sharepoint.com/personal/user_domain_com/Documents/folder")
        assert site == "https://tenant-my.sharepoint.com/personal/user_domain_com"

    def test_sharing_link_folder(self):
        url = (
            "https://epltechfrontltd-my.sharepoint.com"
            "/:f:/g/personal/venizelos_epltechfront_com"
            "/IgDE_WC84lhzTa2XJnG4_35GAeVBDaoMY4jTZw57TqjhUPc?e=IoEIQj"
        )
        site, sub = self.resolve(url)
        assert site == ("https://epltechfrontltd-my.sharepoint.com/personal/venizelos_epltechfront_com")
        assert sub == ""

    def test_sharing_link_file(self):
        url = "https://tenant-my.sharepoint.com/:x:/g/personal/user_domain_com/abc123?e=token"
        site, sub = self.resolve(url)
        assert site == "https://tenant-my.sharepoint.com/personal/user_domain_com"

    def test_sharing_link_word(self):
        url = "https://tenant-my.sharepoint.com/:w:/g/personal/user_domain_com/abc123"
        site, sub = self.resolve(url)
        assert site == "https://tenant-my.sharepoint.com/personal/user_domain_com"

    def test_sharing_link_team_site(self):
        url = "https://contoso.sharepoint.com/:f:/g/sites/MySite/abc123?e=token"
        site, sub = self.resolve(url)
        assert site == "https://contoso.sharepoint.com/sites/MySite"

    def test_bare_domain_passthrough(self):
        url = "https://contoso.sharepoint.com"
        assert self.resolve(url) == (url, "")


class TestExtractSharedFolder:
    extract = staticmethod(extract_shared_folder)
    site = "https://tenant-my.sharepoint.com/personal/user_domain_com"

    def test_folder_link(self):
        url = f"{self.site}/_layouts/15/onedrive.aspx?id=/personal/user_domain_com/Documents/Projects/2026&ga=1"
        assert self.extract(url, self.site) == "Projects/2026"

    def test_file_link_returns_parent(self):
        url = f"{self.site}/_layouts/15/onedrive.aspx?id=/personal/user_domain_com/Documents/Reports/report.docx&ga=1"
        assert self.extract(url, self.site) == "Reports"

    def test_file_in_root_returns_empty(self):
        url = f"{self.site}/_layouts/15/onedrive.aspx?id=/personal/user_domain_com/Documents/readme.txt&ga=1"
        assert self.extract(url, self.site) == ""

    def test_no_id_param(self):
        url = f"{self.site}/_layouts/15/onedrive.aspx?ga=1"
        assert self.extract(url, self.site) == ""


class TestDetectDocLibraryFallback:
    """Test the fallback behavior (no network calls)."""

    @staticmethod
    def _call_with_failing_auth(site_url):
        """Call detect_doc_library with auth that will fail (no server)."""

        class _FailAuth:
            def auth_flow(self, request):
                yield request

        return detect_doc_library(site_url, _FailAuth())

    def test_fallback_personal_site(self):
        result = self._call_with_failing_auth("https://tenant-my.sharepoint.com/personal/user_domain_com")
        assert result == "Documents"

    def test_fallback_team_site(self):
        result = self._call_with_failing_auth("https://contoso.sharepoint.com/sites/MySite")
        assert result == "Shared Documents"
