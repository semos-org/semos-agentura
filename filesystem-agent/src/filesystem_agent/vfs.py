"""Virtual filesystem with symbolic URI addressing over multiple fsspec roots."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import fsspec
from fsspec import AbstractFileSystem
from fsspec.implementations.dirfs import DirFileSystem

ARCHIVE_EXTENSIONS = {".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz"}
ARCHIVE_SEPARATOR = "!"  # e.g. local://docs/data.zip!/inner/path (jar: URI convention)


class _HttpRangeFile:
    """Seekable file-like object backed by HTTP range requests (via httpx).

    Allows ``zipfile.ZipFile`` to read just the central directory and
    individual entries without downloading the entire archive.
    """

    def __init__(self, url: str, headers: dict[str, str] | None = None) -> None:
        import httpx

        self._url = url
        self._client = httpx.Client(headers=headers or {}, follow_redirects=True, timeout=60)
        # Get file size via HEAD
        r = self._client.head(url)
        r.raise_for_status()
        self._size = int(r.headers.get("content-length", 0))
        self._pos = 0

    def read(self, n: int = -1) -> bytes:
        if self._pos >= self._size:
            return b""
        if n == -1 or n is None:
            end = self._size - 1
        else:
            end = min(self._pos + n - 1, self._size - 1)
        r = self._client.get(self._url, headers={"Range": f"bytes={self._pos}-{end}"})
        if r.status_code not in (200, 206):
            r.raise_for_status()
        data = r.content
        self._pos += len(data)
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        elif whence == 2:
            self._pos = self._size + offset
        self._pos = max(0, min(self._pos, self._size))
        return self._pos

    def tell(self) -> int:
        return self._pos

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class SingleFileWebdavFS(AbstractFileSystem):
    """Virtual filesystem exposing a single WebDAV file as a browsable directory.

    Used for single-file sharing links where the parent folder is not accessible
    but the individual file can be read/written via direct HTTP GET/PUT.
    """

    protocol = "single-webdav"

    def __init__(self, file_url, filename, auth, **kwargs):
        super().__init__(**kwargs)
        self._file_url = file_url
        self._filename = filename
        self._auth = auth

    def _get_client(self):
        import httpx

        return httpx.Client(auth=self._auth, follow_redirects=True, timeout=60)

    def ls(self, path, detail=True, **kwargs):
        path = path.strip("/")
        if path in ("", self._filename):
            info = self.info(self._filename)
            if path == "":
                return [info] if detail else [self._filename]
            return info if detail else self._filename
        raise FileNotFoundError(path)

    def info(self, path, **kwargs):
        path = path.strip("/")
        if path == "":
            return {"name": "", "type": "directory", "size": 0}
        if path == self._filename:
            size = 0
            try:
                with self._get_client() as c:
                    r = c.head(self._file_url)
                    if r.status_code == 200:
                        size = int(r.headers.get("content-length", 0))
            except Exception:
                pass
            return {"name": self._filename, "type": "file", "size": size}
        raise FileNotFoundError(path)

    def cat_file(self, path, **kwargs):
        with self._get_client() as c:
            r = c.get(self._file_url)
            r.raise_for_status()
            return r.content

    def pipe_file(self, path, value, **kwargs):
        with self._get_client() as c:
            r = c.put(self._file_url, content=value)
            r.raise_for_status()

    def _open(self, path, mode="rb", **kwargs):
        import io

        if "r" in mode:
            return io.BytesIO(self.cat_file(path))
        raise NotImplementedError(f"Mode {mode} not supported for single-file WebDAV")


@dataclass
class Root:
    """A named mount point in the virtual filesystem."""

    name: str
    fs: AbstractFileSystem
    base_path: str = ""


class VirtualFileSystem:
    """Multiplexes named roots over different fsspec backends.

    Every file and directory is addressed by a symbolic URI: ``{root}://{path}``.
    """

    def __init__(self, on_roots_changed: Any | None = None) -> None:
        self._roots: dict[str, Root] = {}
        self._on_roots_changed = on_roots_changed

    # -- root management --

    def add_root(self, name: str, fs: AbstractFileSystem, base_path: str = "") -> None:
        self._roots[name] = Root(name=name, fs=fs, base_path=base_path)
        if self._on_roots_changed:
            self._on_roots_changed()

    def add_root_from_protocol(self, name: str, protocol: str, base_path: str = "", **kwargs: Any) -> None:
        """Mount a new root using any fsspec-supported protocol.

        Uses ``fsspec.filesystem(protocol, **kwargs)`` as the universal factory.

        Note: some protocols (e.g. "memory") require a non-empty base_path
        (use "/" as minimum) because DirFileSystem cannot scope to None.
        """
        fs = fsspec.filesystem(protocol, **kwargs)
        self.add_root(name, fs, base_path=base_path)

    def remove_root(self, name: str) -> None:
        del self._roots[name]
        if self._on_roots_changed:
            self._on_roots_changed()

    def get_root(self, name: str) -> Root:
        if name not in self._roots:
            raise FileNotFoundError(f"No root named {name!r}")
        return self._roots[name]

    @property
    def roots(self) -> list[str]:
        return list(self._roots.keys())

    def roots_info(self) -> list[dict[str, str]]:
        """Return info about all mounted roots (name, protocol, base_path)."""
        result = []
        for name, root in self._roots.items():
            protocol = root.fs.protocol
            if isinstance(protocol, (list, tuple)):
                protocol = protocol[0]
            result.append({"name": name, "protocol": protocol, "base_path": root.base_path})
        return result

    # -- URI helpers --

    @staticmethod
    def parse_uri(uri: str) -> tuple[str, str]:
        """``"local://docs/f.txt"`` → ``("local", "docs/f.txt")``."""
        if "://" not in uri:
            raise ValueError(f"Invalid URI (missing '://'): {uri!r}")
        scheme, _, path = uri.partition("://")
        return scheme, path.strip("/")

    @staticmethod
    def make_uri(root_name: str, rel_path: str = "") -> str:
        """``("local", "docs/f.txt")`` → ``"local://docs/f.txt"``."""
        rel = rel_path.strip("/")
        return f"{root_name}://{rel}" if rel else f"{root_name}://"

    # -- internal resolution --

    def _get_scoped_fs(self, root_name: str) -> AbstractFileSystem:
        if root_name not in self._roots:
            raise FileNotFoundError(f"No root named {root_name!r}")
        root = self._roots[root_name]
        # Self-scoped filesystems (e.g. SingleFileWebdavFS) don't need DirFileSystem
        if isinstance(root.fs, SingleFileWebdavFS) or not root.base_path:
            return root.fs
        return DirFileSystem(path=root.base_path, fs=root.fs, skip_instance_cache=True)

    def _resolve(self, uri: str) -> tuple[str, DirFileSystem, str]:
        """Return (root_name, scoped_fs, relative_path)."""
        root_name, rel = self.parse_uri(uri)
        return root_name, self._get_scoped_fs(root_name), rel

    # -- read operations --

    def ls(self, uri: str = "", detail: bool = True) -> list[dict[str, Any]] | list[str]:
        """List entries.  Empty/missing URI lists all roots."""
        if not uri or uri == "":
            if detail:
                return [{"uri": self.make_uri(n), "name": n, "type": "directory", "size": 0} for n in self._roots]
            return [self.make_uri(n) for n in self._roots]

        root_name, fs, rel = self._resolve(uri)
        entries = fs.ls(rel or "", detail=True)

        results: list[dict[str, Any]] = []
        for e in entries:
            raw_name = e["name"] if isinstance(e, dict) else e
            basename = PurePosixPath(str(raw_name)).name
            entry_uri = self.make_uri(root_name, f"{rel}/{basename}".strip("/"))
            if detail:
                info = e if isinstance(e, dict) else {"name": raw_name}
                entry_dict: dict[str, Any] = {
                    "uri": entry_uri,
                    "name": basename,
                    "type": info.get("type") or "file",
                    "size": info.get("size") or 0,
                }
                for ts_key in ("modified", "created", "mtime"):
                    if info.get(ts_key):
                        entry_dict["modified"] = info[ts_key]
                        break
                results.append(entry_dict)
            else:
                results.append(entry_uri)
        return results

    def info(self, uri: str) -> dict[str, Any]:
        root_name, rel = self.parse_uri(uri)
        if not rel:
            return {"uri": uri, "name": root_name, "type": "directory", "size": 0}
        fs = self._get_scoped_fs(root_name)
        try:
            raw = fs.info(rel)
        except FileNotFoundError:
            # WebDAV fallback: HEAD request to get size/type
            url = self._get_http_url(fs, rel)
            if url:
                raw_fs = fs.fs if hasattr(fs, "fs") else fs
                r = raw_fs.client.http.head(url, follow_redirects=True)
                if r.status_code == 200:
                    raw = {
                        "name": rel,
                        "type": "file",
                        "size": int(r.headers.get("content-length", 0)),
                    }
                else:
                    raise
            else:
                raise
        basename = PurePosixPath(str(raw.get("name", rel))).name
        result: dict[str, Any] = {
            "uri": uri,
            "name": basename,
            "type": raw.get("type", "file"),
            "size": raw.get("size", 0),
        }
        for ts_key in ("modified", "created", "mtime"):
            if raw.get(ts_key):
                result["modified"] = raw[ts_key]
                break
        return result

    def isdir(self, uri: str) -> bool:
        return self.info(uri).get("type") == "directory"

    def cat(self, uri: str) -> bytes:
        if ARCHIVE_SEPARATOR in uri:
            return self.cat_archive(uri)
        return self._cat_robust(uri)

    def tree(self, uri: str = "", depth: int = 1) -> list[dict[str, Any]]:
        """Return a nested tree structure pre-loaded to *depth* levels.

        Each node is a dict with keys: uri, name, type, size, children.
        ``children`` is a list of child nodes for directories (up to *depth*),
        or ``None`` for files.  Directories beyond *depth* get ``children=[]``
        (empty list signals "expandable but not yet loaded").
        """
        entries = self.ls(uri, detail=True)
        nodes = []
        for entry in entries:
            node = {**entry, "children": None}
            if entry["type"] == "directory":
                if depth > 0:
                    try:
                        node["children"] = self.tree(entry["uri"], depth=depth - 1)
                    except Exception:
                        node["children"] = []  # failed - show as expandable placeholder
                else:
                    node["children"] = []  # lazy-loadable placeholder
            nodes.append(node)
        return nodes

    # -- write operations --

    def mkdir(self, uri: str) -> None:
        _, fs, rel = self._resolve(uri)
        fs.mkdir(rel, create_parents=True)

    def put(self, uri: str, data: bytes) -> None:
        _, fs, rel = self._resolve(uri)
        try:
            with fs.open(rel, "wb") as f:
                f.write(data)
        except (FileNotFoundError, OSError, Exception) as exc:
            # WebDAV fallback: HTTP PUT directly
            url = self._get_http_url(fs, rel)
            if url:
                raw_fs = fs.fs if hasattr(fs, "fs") else fs
                r = raw_fs.client.http.put(url, content=data, follow_redirects=True)
                r.raise_for_status()
            else:
                raise exc

    def rm(self, uri: str, recursive: bool = False) -> None:
        _, fs, rel = self._resolve(uri)
        try:
            fs.rm(rel, recursive=recursive)
        except (FileNotFoundError, OSError, Exception) as exc:
            # WebDAV on SharePoint: PROPFIND on individual files may fail.
            # Fall back to HTTP DELETE via the underlying client.
            url = self._get_http_url(fs, rel)
            if url:
                raw_fs = fs.fs if hasattr(fs, "fs") else fs
                r = raw_fs.client.http.request("DELETE", url, follow_redirects=True)
                if r.status_code not in (200, 204, 404):
                    r.raise_for_status()
            else:
                raise exc

    def mv(self, src_uri: str, dst_uri: str) -> None:
        src_root, _, src_rel = self._resolve(src_uri)
        dst_root, _, dst_rel = self._resolve(dst_uri)
        if src_root == dst_root:
            fs = self._get_scoped_fs(src_root)
            fs.mv(src_rel, dst_rel)
        else:
            data = self.cat(src_uri)
            self.put(dst_uri, data)
            self.rm(src_uri)

    def cp(self, src_uri: str, dst_uri: str) -> None:
        # Handle archive sources transparently
        if ARCHIVE_SEPARATOR in src_uri:
            data = self.cat_archive(src_uri)
            if ARCHIVE_SEPARATOR in dst_uri:
                self.put_archive(dst_uri, data)
            else:
                self.put(dst_uri, data)
            return
        if ARCHIVE_SEPARATOR in dst_uri:
            data = self.cat(src_uri)
            self.put_archive(dst_uri, data)
            return
        src_root, _, src_rel = self._resolve(src_uri)
        dst_root, _, dst_rel = self._resolve(dst_uri)
        if src_root == dst_root:
            fs = self._get_scoped_fs(src_root)
            fs.cp(src_rel, dst_rel)
        else:
            data = self.cat(src_uri)
            self.put(dst_uri, data)

    # -- archive support --

    @staticmethod
    def is_archive(uri: str) -> bool:
        """Check if a URI points to a supported archive file (not inside one)."""
        if ARCHIVE_SEPARATOR in uri:
            return False  # this is a path INSIDE an archive, not an archive itself
        lower = uri.lower()
        return any(lower.endswith(ext) for ext in ARCHIVE_EXTENSIONS)

    @staticmethod
    def split_archive_uri(uri: str) -> tuple[str, str]:
        """Split ``local://docs/a.zip!/inner/path`` → ``("local://docs/a.zip", "inner/path")``."""
        if ARCHIVE_SEPARATOR in uri:
            archive_uri, inner = uri.split(ARCHIVE_SEPARATOR, 1)
            return archive_uri, inner.strip("/")
        return uri, ""

    def _open_archive_fs(self, archive_uri: str) -> AbstractFileSystem:
        """Open an archive and return an fsspec filesystem for its contents.

        For remote backends that support HTTP range requests, uses a seekable
        HTTP file object so only the zip central directory + requested entries
        are downloaded (not the entire archive).
        """
        import io

        _, fs, rel = self._resolve(archive_uri)

        # Try to get a direct HTTP URL for range-based access
        http_url = self._get_http_url(fs, rel)
        if http_url:
            fo = _HttpRangeFile(http_url, self._get_http_headers(fs))
        else:
            # Fallback: download entire archive into memory
            data = self._cat_robust(archive_uri)
            fo = io.BytesIO(data)

        lower = archive_uri.lower()
        if lower.endswith(".zip"):
            protocol = "zip"
        elif any(lower.endswith(e) for e in (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")):
            protocol = "tar"
        else:
            raise ValueError(f"Unsupported archive format: {archive_uri}")
        return fsspec.filesystem(protocol, fo=fo)

    @staticmethod
    def _get_http_url(fs: AbstractFileSystem, rel: str) -> str | None:
        """Try to construct a direct HTTP URL for a file on a WebDAV backend."""
        raw_fs = fs.fs if hasattr(fs, "fs") else fs
        if hasattr(raw_fs, "client") and hasattr(raw_fs.client, "base_url"):
            full_path = fs._join(rel) if hasattr(fs, "_join") else rel
            return f"{raw_fs.client.base_url}/{full_path.lstrip('/')}"
        return None

    @staticmethod
    def _get_http_headers(fs: AbstractFileSystem) -> dict[str, str]:
        """Extract auth headers from a WebDAV filesystem."""
        raw_fs = fs.fs if hasattr(fs, "fs") else fs
        if hasattr(raw_fs, "client") and hasattr(raw_fs.client, "http"):
            http_client = raw_fs.client.http
            headers = dict(http_client.headers)
            # CookieAuth stores cookies in auth_flow, not in default headers.
            # Extract the cookie_header from the auth object directly.
            auth = getattr(http_client, "_auth", None)
            if auth and hasattr(auth, "cookie_header"):
                headers["Cookie"] = auth.cookie_header
            return headers
        return {}

    def _cat_robust(self, uri: str) -> bytes:
        """Read file contents, with fallback for WebDAV backends.

        webdav4 on SharePoint fails PROPFIND on individual files.
        Falls back to HTTP GET via the underlying client when cat() fails.
        """
        _, fs, rel = self._resolve(uri)
        try:
            return fs.cat(rel)
        except FileNotFoundError:
            # Try direct HTTP GET via the underlying WebDAV client
            url = self._get_http_url(fs, rel)
            if url:
                raw_fs = fs.fs if hasattr(fs, "fs") else fs
                r = raw_fs.client.http.get(url, follow_redirects=True)
                if r.status_code == 200:
                    return r.content
            raise

    def ls_archive(self, uri: str, detail: bool = True) -> list[dict[str, Any]] | list[str]:
        """List entries inside an archive.

        *uri* can be:
        - ``local://docs/a.zip``  - list root of archive
        - ``local://docs/a.zip!/data``  - list ``data/`` folder inside archive
        """
        archive_uri, inner = self.split_archive_uri(uri)
        afs = self._open_archive_fs(archive_uri)
        entries = afs.ls(inner or "", detail=True)

        results: list[dict[str, Any]] = []
        for e in entries:
            raw_name = e["name"] if isinstance(e, dict) else e
            basename = PurePosixPath(str(raw_name)).name
            inner_path = f"{inner}/{basename}".strip("/") if inner else basename
            entry_uri = f"{archive_uri}{ARCHIVE_SEPARATOR}{inner_path}"
            if detail:
                info = e if isinstance(e, dict) else {"name": raw_name}
                results.append(
                    {
                        "uri": entry_uri,
                        "name": basename,
                        "type": info.get("type") or "file",
                        "size": info.get("size") or 0,
                    }
                )
            else:
                results.append(entry_uri)
        return results

    def cat_archive(self, uri: str) -> bytes:
        """Read a file inside an archive."""
        archive_uri, inner = self.split_archive_uri(uri)
        afs = self._open_archive_fs(archive_uri)
        return afs.cat(inner)

    def archive_size(self, archive_uri: str) -> int:
        """Return the size in bytes of the archive file on disk."""
        outer = archive_uri.split(ARCHIVE_SEPARATOR, 1)[0]
        return self.info(outer).get("size", 0)

    def put_archive(self, uri: str, data: bytes) -> None:
        """Write *data* into the archive at the inner path.

        Rebuilds the entire archive (zip only for now).
        """
        import io
        import zipfile

        archive_uri, inner = self.split_archive_uri(uri)
        if not inner:
            raise ValueError("No inner path specified")

        # Read existing archive bytes (robust: handles WebDAV)
        existing = self._cat_robust(archive_uri)

        # Rebuild zip in memory with the new/replaced file
        _, fs, rel = self._resolve(archive_uri)
        buf = io.BytesIO()
        with (
            zipfile.ZipFile(io.BytesIO(existing), "r") as zin,
            zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zout,
        ):
            for item in zin.infolist():
                if item.filename.rstrip("/") == inner.rstrip("/"):
                    continue  # skip  - will be replaced
                zout.writestr(item, zin.read(item.filename))
            zout.writestr(inner, data)

        # Write rebuilt archive back
        with fs.open(rel, "wb") as f:
            f.write(buf.getvalue())

    def rm_archive(self, uri: str) -> None:
        """Remove a file or folder from inside an archive.

        Rebuilds the entire archive (zip only for now).
        """
        import io
        import zipfile

        archive_uri, inner = self.split_archive_uri(uri)
        if not inner:
            raise ValueError("No inner path specified")

        existing = self._cat_robust(archive_uri)

        _, fs, rel = self._resolve(archive_uri)
        buf = io.BytesIO()
        prefix = inner.rstrip("/") + "/"
        with (
            zipfile.ZipFile(io.BytesIO(existing), "r") as zin,
            zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zout,
        ):
            for item in zin.infolist():
                name = item.filename.rstrip("/")
                # Skip exact match or anything under the folder
                if name == inner.rstrip("/") or item.filename.startswith(prefix):
                    continue
                zout.writestr(item, zin.read(item.filename))

        with fs.open(rel, "wb") as f:
            f.write(buf.getvalue())
