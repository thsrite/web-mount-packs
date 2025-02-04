#!/usr/bin/env python3
# encoding: utf-8

from __future__ import annotations

__author__ = "ChenyangGao <https://chenyanggao.github.io>"
__all__ = ["P115SharePath", "P115ShareFileSystem"]

import errno

from collections import deque
from collections.abc import (
    AsyncIterator, Awaitable, Callable, Iterable, Iterator, Mapping, 
    MutableMapping, Sequence, 
)
from copy import deepcopy
from datetime import datetime
from functools import cached_property, partial
from os import fspath, stat_result, PathLike
from posixpath import join as joinpath
from re import compile as re_compile
from stat import S_IFDIR, S_IFREG
from time import time
from typing import cast, overload, Literal, Never, Self

from iterutils import run_gen_step
from posixpatht import escape, joins, splits, path_is_dir_form

from .client import check_response, P115Client, P115Url
from .fs_base import AttrDict, IDOrPathType, P115PathBase, P115FileSystemBase


CRE_SHARE_LINK_search = re_compile(r"(?:/s/|share\.115\.com/)(?P<share_code>[a-z0-9]+)(\?password=(?P<receive_code>\w+))?").search


def normalize_info(
    info: Mapping, 
    keep_raw: bool = False, 
    **extra_data, 
) -> AttrDict:
    if "fid" in info:
        fid = info["fid"]
        parent_id = info["cid"]
        is_directory = False
    else:
        fid = info["cid"]
        parent_id = info["pid"]
        is_directory = True
    info2 =  {
        "name": info["n"], 
        "is_directory": is_directory, 
        "size": info.get("s"), 
        "id": int(fid), 
        "parent_id": int(parent_id), 
        "sha1": info.get("sha"), 
    }
    timestamp = info2["timestamp"] = int(info["t"])
    info2["time"] = datetime.fromtimestamp(timestamp)
    if "pc" in info:
        info2["pickcode"] = info["pc"]
    if "fl" in info:
        info2["labels"] = info["fl"]
    if "c" in info:
        info2["violated"] = bool(info["c"])
    if "u" in info:
        info2["thumb"] = info["u"]
    if "play_long" in info:
        info2["play_long"] = info["play_long"]
    info2["ico"] = info.get("ico", "folder" if is_directory else "")
    if keep_raw:
        info2["raw"] = info
    if extra_data:
        info2.update(extra_data)
    return info2


class P115SharePath(P115PathBase):
    fs: P115ShareFileSystem


class P115ShareFileSystem(P115FileSystemBase[P115SharePath]):
    share_link: str
    share_code: str
    receive_code: str
    path_to_id: MutableMapping[str, int]
    id_to_attr: MutableMapping[int, AttrDict]
    pid_to_children: MutableMapping[int, tuple[AttrDict, ...]]
    full_loaded: bool
    path_class = P115SharePath

    def __init__(
        self, 
        /, 
        client: str | P115Client, 
        share_link: str, 
        request: None | Callable = None, 
        async_request: None | Callable = None, 
    ):
        m = CRE_SHARE_LINK_search(share_link)
        if m is None:
            raise ValueError("not a valid 115 share link")
        super().__init__(client, request, async_request)
        self.__dict__.update(
            id=0, 
            path="/", 
            share_link=share_link, 
            share_code=m["share_code"], 
            receive_code= m["receive_code"] or "", 
            path_to_id={"/": 0}, 
            id_to_attr={}, 
            pid_to_children={}, 
            full_loaded=False, 
        )

    def __repr__(self, /) -> str:
        cls = type(self)
        module = cls.__module__
        name = cls.__qualname__
        if module != "__main__":
            name = module + "." + name
        return f"<{name}(client={self.client!r}, share_link={self.share_link!r}, id={self.id!r}, path={self.path!r}) at {hex(id(self))}>"

    def __setattr__(self, attr, val, /) -> Never:
        raise TypeError("can't set attributes")

    @overload
    def fs_files(
        self, 
        /, 
        payload: dict, 
        async_: Literal[False] = False, 
    ) -> dict:
        ...
    @overload
    def fs_files(
        self, 
        /, 
        payload: dict, 
        async_: Literal[True], 
    ) -> Awaitable[dict]:
        ...
    def fs_files(
        self, 
        /, 
        payload: dict, 
        async_: Literal[False, True] = False, 
    ) -> dict | Awaitable[dict]:
        """获取分享链接的某个文件夹中的文件和子文件夹的列表（包含详细信息）
        :param payload:
            - id: int | str = 0
            - limit: int = 32
            - offset: int = 0
            - asc: 0 | 1 = <default> # 是否升序排列
            - o: str = <default>
                # 用某字段排序：
                # - 文件名："file_name"
                # - 文件大小："file_size"
                # - 文件种类："file_type"
                # - 修改时间："user_utime"
                # - 创建时间："user_ptime"
                # - 上次打开时间："user_otime"
        """
        return check_response(self.client.share_snap( # type: ignore
            {
                **payload, 
                "share_code": self.share_code, 
                "receive_code": self.receive_code, 
            }, 
            request=self.async_request if async_ else self.request, 
            async_=async_, 
        ))

    @overload
    def downlist(
        self, 
        /, 
        id: int = 0, 
        *, 
        async_: Literal[False] = False, 
    ) -> dict:
        ...
    @overload
    def downlist(
        self, 
        /, 
        id: int = 0, 
        *, 
        async_: Literal[True], 
    ) -> Awaitable[dict]:
        ...
    def downlist(
        self, 
        /, 
        id: int = 0, 
        *, 
        async_: Literal[False, True] = False, 
    ) -> dict | Awaitable[dict]:
        """获取分享链接的某个文件夹中可下载的文件的列表（只含文件，不含文件夹，任意深度，简略信息）
        """
        return check_response(self.client.share_downlist( # type: ignore
            {
                "share_code": self.share_code, 
                "receive_code": self.receive_code, 
                "cid": id, 
            }, 
            request=self.async_request if async_ else self.request, 
            async_=async_, 
        ))

    @cached_property
    def create_time(self, /) -> datetime:
        "分享的创建时间"
        return datetime.fromtimestamp(int(self.shareinfo["create_time"]))

    @cached_property
    def snap_id(self, /) -> int:
        "获取这个分享的 id"
        return int(self.shareinfo["snap_id"])

    @cached_property
    def user_id(self, /) -> int:
        "获取分享者的用户 id"
        return int(self.sharedata["userinfo"]["user_id"])

    @property
    def sharedata(self, /) -> dict:
        "获取分享的首页数据"
        return self.fs_files({"limit": 1})["data"]

    @property
    def shareinfo(self, /) -> dict:
        "获取分享信息"
        return self.sharedata["shareinfo"]

    @overload
    def _search_item(
        self, 
        id: int, 
        /, 
        async_: Literal[False] = False, 
    ) -> AttrDict:
        ...
    @overload
    def _search_item(
        self, 
        id: int, 
        /, 
        async_: Literal[True], 
    ) -> Awaitable[AttrDict]:
        ...
    def _search_item(
        self, 
        id: int, 
        /, 
        async_: Literal[False, True] = False, 
    ) -> AttrDict | Awaitable[AttrDict]:
        dq = deque((self.attr(0),))
        get, put = dq.popleft, dq.append
        if async_:
            async def request():
                while dq:
                    async for attr in self.iterdir(get(), async_=True):
                        if attr["id"] == id:
                            return attr
                        if attr["is_directory"]:
                            put(attr)
                self.__dict__["full_loaded"] = True
                raise FileNotFoundError(errno.ENOENT, f"no such id: {id!r}")
            return request()
        else:
            while dq:
                for attr in self.iterdir(get()):
                    if attr["id"] == id:
                        return attr
                    if attr["is_directory"]:
                        put(attr)
            self.__dict__["full_loaded"] = True
            raise FileNotFoundError(errno.ENOENT, f"no such id: {id!r}")

    @overload
    def _attr(
        self, 
        id: int, 
        /, 
        async_: Literal[False] = False, 
    ) -> AttrDict:
        ...
    @overload
    def _attr(
        self, 
        id: int, 
        /, 
        async_: Literal[True], 
    ) -> Awaitable[AttrDict]:
        ...
    def _attr(
        self, 
        id: int, 
        /, 
        async_: Literal[False, True] = False, 
    ) -> AttrDict | Awaitable[AttrDict]:
        def gen_step():
            try:
                return self.id_to_attr[id]
            except KeyError:
                pass
            if self.full_loaded:
                raise FileNotFoundError(errno.ENOENT, f"no such id: {id!r}")
            if id == 0:
                attr = self.id_to_attr[0] = {
                    "id": 0, 
                    "parent_id": 0, 
                    "name": "", 
                    "path": "/", 
                    "is_directory": True, 
                    "size": None, 
                    "time": self.create_time, 
                    "timestamp": int(self.create_time.timestamp()), 
                    "ico": "folder", 
                    "fs": self, 
                    "ancestors": [{"id": 0, "name": ""}], 
                }
                return attr
            # NOTE: quick detection of id existence
            yield partial(
                self.client.share_download_url, 
                {
                    "share_code": self.share_code, 
                    "receive_code": self.receive_code, 
                    "file_id": id, 
                }, 
                strict=False, 
                request=self.async_request if async_ else self.request, 
                async_=async_, 
            )
            return (yield partial(self._search_item, id, async_=async_))
        return run_gen_step(gen_step, async_=async_)

    @overload
    def _attr_path(
        self, 
        path: str | PathLike[str] | Sequence[str], 
        /, 
        pid: None | int = None, 
        force_directory: bool = False, 
        *, 
        async_: Literal[False] = False, 
    ) -> AttrDict:
        ...
    @overload
    def _attr_path(
        self, 
        path: str | PathLike[str] | Sequence[str], 
        /, 
        pid: None | int = None, 
        force_directory: bool = False, 
        *, 
        async_: Literal[True], 
    ) -> Awaitable[AttrDict]:
        ...
    def _attr_path(
        self, 
        path: str | PathLike[str] | Sequence[str], 
        /, 
        pid: None | int = None, 
        force_directory: bool = False, 
        *, 
        async_: Literal[False, True] = False, 
    ) -> AttrDict | Awaitable[AttrDict]:
        def gen_step():
            nonlocal path, pid, force_directory

            if isinstance(path, PathLike):
                path = fspath(path)
            if pid is None:
                pid = self.id
            if not path or path == ".":
                return (yield partial(self._attr, pid, async_=async_))

            parents = 0
            if isinstance(path, str):
                if not force_directory:
                    force_directory = path_is_dir_form(path)
                patht, parents = splits(path)
                if not (patht or parents):
                    return (yield partial(self._attr, pid, async_=async_))
            else:
                if not force_directory:
                    force_directory = path[-1] == ""
                patht = [path[0], *(p for p in path[1:] if p)]
            if patht == [""]:
                return self._attr(0)
            elif patht and patht[0] == "":
                pid = 0

            ancestor_patht: list[str] = []
            if pid == 0:
                if patht[0] != "":
                    patht.insert(0, "")
            else:
                ancestors = yield partial(self.get_ancestors, pid, async_=async_)
                if parents:
                    if parents >= len(ancestors):
                        pid = 0
                    else:
                        pid = cast(int, ancestors[-parents-1]["id"])
                        ancestor_patht = ["", *(a["name"] for a in ancestors[1:-parents])]
                else:
                    ancestor_patht = ["", *(a["name"] for a in ancestors[1:])]
            if not patht:
                return (yield partial(self._attr, pid, async_=async_))

            if pid == 0:
                dirname = ""
                ancestors_paths: list[str] = [(dirname := f"{dirname}/{escape(name)}") for name in patht[1:]]
            else:
                dirname = joins(ancestor_patht)
                ancestors_paths = [(dirname := f"{dirname}/{escape(name)}") for name in patht]

            fullpath = ancestors_paths[-1]
            path_to_id = self.path_to_id
            if path_to_id:
                if not force_directory and (id := path_to_id.get(fullpath)):
                    return (yield partial(self._attr, id, async_=async_))
                if (id := path_to_id.get(fullpath + "/")):
                    return (yield partial(self._attr, id, async_=async_))
            if self.full_loaded:
                raise FileNotFoundError(
                    errno.ENOENT, 
                    f"no such path {fullpath!r} (in {pid!r})", 
                )

            parent: int | AttrDict
            for i in reversed(range(len(ancestors_paths)-1)):
                if path_to_id and (id := path_to_id.get((dirname := ancestors_paths[i]) + "/")):
                    parent = yield partial(self._attr, id, async_=async_)
                    i += 1
                    break
            else:
                i = 0
                parent = pid

            if pid == 0:
                i += 1

            attr: AttrDict
            last_idx = len(patht) - 1
            if async_:
                for i, name in enumerate(patht[i:], i):
                    async def step():
                        nonlocal attr, parent
                        async for attr in self.iterdir(parent, async_=True):
                            if attr["name"] == name:
                                if force_directory or i < last_idx:
                                    if attr["is_directory"]:
                                        parent = attr
                                        break
                                else:
                                    break
                        else:
                            if isinstance(parent, AttrDict):
                                parent = parent["id"]
                            raise FileNotFoundError(
                                errno.ENOENT, 
                                f"no such file {name!r} (in {parent} @ {joins(patht[:i])!r})", 
                            )
                    yield step
            else:
                for i, name in enumerate(patht[i:], i):
                    for attr in self.iterdir(parent):
                        if attr["name"] == name:
                            if force_directory or i < last_idx:
                                if attr["is_directory"]:
                                    parent = attr
                                    break
                            else:
                                break
                    else:
                        if isinstance(parent, AttrDict):
                            parent = parent["id"]
                        raise FileNotFoundError(
                            errno.ENOENT, 
                            f"no such file {name!r} (in {parent} @ {joins(patht[:i])!r})", 
                        )
            return attr
        return run_gen_step(gen_step, async_=async_)

    @overload
    def attr(
        self, 
        id_or_path: IDOrPathType = "", 
        /, 
        pid: None | int = None, 
        force_directory: bool = False, 
        *, 
        async_: Literal[False] = False, 
    ) -> AttrDict:
        ...
    @overload
    def attr(
        self, 
        id_or_path: IDOrPathType = "", 
        /, 
        pid: None | int = None, 
        force_directory: bool = False, 
        *, 
        async_: Literal[True], 
    ) -> Awaitable[AttrDict]:
        ...
    def attr(
        self, 
        id_or_path: IDOrPathType = "", 
        /, 
        pid: None | int = None, 
        force_directory: bool = False, 
        *, 
        async_: Literal[False, True] = False, 
    ) -> AttrDict | Awaitable[AttrDict]:
        "获取属性"
        def gen_step():
            path_class = type(self).path_class
            if isinstance(id_or_path, path_class):
                attr = id_or_path.__dict__
            elif isinstance(id_or_path, AttrDict):
                attr = id_or_path
            elif isinstance(id_or_path, int):
                attr = yield partial(self._attr, id_or_path, async_=async_)
            else:
                return (yield partial(
                    self._attr_path, 
                    id_or_path, 
                    pid=pid, 
                    force_directory=force_directory, 
                    async_=async_, 
                ))
            if force_directory and not attr["is_directory"]:
                raise NotADirectoryError(
                    errno.ENOTDIR, 
                    f"{attr['id']} (id={attr['id']}) is not directory"
                )
            return attr
        return run_gen_step(gen_step, async_=async_)

    @overload
    def dirlen(
        self, 
        id_or_path: IDOrPathType = "", 
        /, 
        pid: None | int = None, 
        *, 
        async_: Literal[False] = False, 
    ) -> int:
        ...
    @overload
    def dirlen(
        self, 
        id_or_path: IDOrPathType = "", 
        /, 
        pid: None | int = None, 
        *, 
        async_: Literal[True], 
    ) -> Awaitable[int]:
        ...
    def dirlen(
        self, 
        id_or_path: IDOrPathType = "", 
        /, 
        pid: None | int = None, 
        *, 
        async_: Literal[False, True] = False, 
    ) -> int | Awaitable[int]:
        "文件夹中的项目数（直属的文件和目录计数）"
        def gen_step():
            id = yield partial(self.get_id, id_or_path, pid=pid, async_=async_)
            if (children := self.pid_to_children.get(id)) is not None:
                return len(children)
            resp = yield partial(
                self.fs_files, 
                {"cid": id, "limit": 1}, 
                async_=async_, 
            )
            return resp["data"]["count"]
        return run_gen_step(gen_step, async_=async_)

    @overload
    def get_ancestors(
        self, 
        id_or_path: IDOrPathType = "", 
        /, 
        pid: None | int = None, 
        *, 
        async_: Literal[False] = False, 
    ) -> list[dict]:
        ...
    @overload
    def get_ancestors(
        self, 
        id_or_path: IDOrPathType = "", 
        /, 
        pid: None | int = None, 
        *, 
        async_: Literal[True], 
    ) -> Awaitable[list[dict]]:
        ...
    def get_ancestors(
        self, 
        id_or_path: IDOrPathType = "", 
        /, 
        pid: None | int = None, 
        *, 
        async_: Literal[False, True] = False, 
    ) -> list[dict] | Awaitable[list[dict]]:
        "获取各个上级目录的少量信息（从根目录到当前目录）"
        def gen_step():
            attr = yield partial(self.attr, id_or_path, pid=pid, async_=async_)
            return deepcopy(attr["ancestors"])
        return run_gen_step(gen_step, async_=async_)

    @overload
    def get_url(
        self, 
        id_or_path: IDOrPathType, 
        /, 
        pid: None | int = None, 
        headers: None | Mapping = None, 
        *, 
        async_: Literal[False] = False, 
    ) -> P115Url:
        ...
    @overload
    def get_url(
        self, 
        id_or_path: IDOrPathType, 
        /, 
        pid: None | int = None, 
        headers: None | Mapping = None, 
        *, 
        async_: Literal[True], 
    ) -> Awaitable[P115Url]:
        ...
    def get_url(
        self, 
        id_or_path: IDOrPathType, 
        /, 
        pid: None | int = None, 
        headers: None | Mapping = None, 
        *, 
        async_: Literal[False, True] = False, 
    ) -> P115Url | Awaitable[P115Url]:
        "获取下载链接"
        def gen_step():
            if isinstance(id_or_path, int):
                id = id_or_path
            else:
                attr = yield partial(self.attr, id_or_path, pid=pid, async_=async_)
                if attr["is_directory"]:
                    raise IsADirectoryError(errno.EISDIR, f"{attr['path']!r} (id={attr['id']!r}) is a directory")
                id = attr["id"]
            return (yield partial(
                self.client.share_download_url, 
                {
                    "share_code": self.share_code, 
                    "receive_code": self.receive_code, 
                    "file_id": id, 
                }, 
                headers=headers, 
                request=self.async_request if async_ else self.request, 
                async_=async_, 
            ))
        return run_gen_step(gen_step, async_=async_)

    @overload
    def iterdir(
        self, 
        id_or_path: IDOrPathType = "", 
        /, 
        pid: None | int = None, 
        start: int = 0, 
        stop: None | int = None, 
        page_size: int = 1_000, 
        *, 
        async_: Literal[False] = False, 
        **payload, 
    ) -> Iterator[AttrDict]:
        ...
    @overload
    def iterdir(
        self, 
        id_or_path: IDOrPathType = "", 
        /, 
        pid: None | int = None, 
        start: int = 0, 
        stop: None | int = None, 
        page_size: int = 1_000, 
        *, 
        async_: Literal[True], 
        **payload, 
    ) -> AsyncIterator[AttrDict]:
        ...
    def iterdir(
        self, 
        id_or_path: IDOrPathType = "", 
        /, 
        pid: None | int = None, 
        start: int = 0, 
        stop: None | int = None, 
        page_size: int = 1_000, 
        *, 
        async_: Literal[False, True] = False, 
        **payload, 
    ) -> Iterator[AttrDict] | AsyncIterator[AttrDict]:
        """迭代获取目录内直属的文件或目录的信息
        :param payload:
            - limit: int = 32
            - offset: int = 0
            - asc: 0 | 1 = <default> # 是否升序排列
            - o: str = <default>
                # 用某字段排序：
                # - 文件名："file_name"
                # - 文件大小："file_size"
                # - 文件种类："file_type"
                # - 修改时间："user_utime"
                # - 创建时间："user_ptime"
                # - 上次打开时间："user_otime"
        """
        path_class = type(self).path_class
        if page_size <= 0:
            page_size = 1_000
        def gen_step():
            nonlocal start, stop
            if stop is not None and (start >= 0 and stop >= 0 or start < 0 and stop < 0) and start >= stop:
                return ()
            if isinstance(id_or_path, int):
                attr = yield partial(self._attr, id_or_path, async_=async_)
            elif isinstance(id_or_path, AttrDict):
                attr = id_or_path
            elif isinstance(id_or_path, path_class):
                attr = id_or_path.__dict__
            else:
                attr = yield partial(
                    self._attr_path, 
                    id_or_path, 
                    pid=pid, 
                    force_directory=True, 
                    async_=async_, 
                )
            if not attr["is_directory"]:
                raise NotADirectoryError(
                    errno.ENOTDIR, 
                    f"{attr['path']!r} (id={attr['id']!r}) is not a directory", 
                )
            id = attr["id"]
            ancestors = attr["ancestors"]
            children: Sequence[AttrDict]
            try:
                children = self.pid_to_children[id]
            except KeyError:
                payload["cid"] = id
                payload["limit"] = page_size
                offset = int(payload.setdefault("offset", 0))
                if offset < 0:
                    offset = payload["offset"] = 0
                else:
                    payload["offset"] = 0
                dirname = attr["path"]
                get_files = self.fs_files
                path_to_id = self.path_to_id
                ls: list[AttrDict] = []
                add = ls.append
                resp = yield partial(get_files, payload, async_=async_)
                data = resp["data"]
                for attr in map(normalize_info, data["list"]):
                    attr["fs"] = self
                    attr["ancestors"] = [*ancestors, {"id": attr["id"], "name": attr["name"]}]
                    path = attr["path"] = joinpath(dirname, escape(attr["name"]))
                    path_to_id[path + "/"[:attr["is_directory"]]] = attr["id"]
                    add(attr)
                for _ in range((data["count"] - 1) // page_size):
                    payload["offset"] += page_size
                    resp = yield partial(get_files, payload, async_=async_)
                    data = resp["data"]
                    for attr in map(normalize_info, data["list"]):
                        attr["fs"] = self
                        attr["ancestors"] = [*ancestors, {"id": attr["id"], "name": attr["name"]}]
                        path = attr["path"] = joinpath(dirname, escape(attr["name"]))
                        path_to_id[path + "/"[:attr["is_directory"]]] = attr["id"]
                        add(attr)
                children = self.pid_to_children[id] = tuple(ls)
                self.id_to_attr.update((attr["id"], attr) for attr in children)
            else:
                count = len(children)
                if start < 0:
                    start += count
                    if start < 0:
                        start = 0
                if stop is None:
                    stop = count
                elif stop < 0:
                    stop += count
                if start >= stop or stop <= 0 or start >= count:
                    return ()
                key: None | Callable
                match payload.get("o"):
                    case "file_name":
                        key = lambda attr: attr["name"]
                    case "file_size":
                        key = lambda attr: attr.get("size") or 0
                    case "file_type":
                        key = lambda attr: attr.get("ico", "folder" if attr["is_directory"] else "")
                    case "user_utime" | "user_ptime" | "user_otime":
                        key = lambda attr: attr["time"]
                    case _:
                        key = None
                if key:
                    children = sorted(children, key=key, reverse=payload.get("asc", True))
            return children[start:stop]
        return run_gen_step(gen_step, async_=async_, as_iter=True)

    @overload
    def receive(
        self, 
        ids: int | str | Iterable[int | str], 
        /, 
        to_pid: int = 0, 
        *, 
        async_: Literal[False] = False, 
    ) -> dict:
        ...
    @overload
    def receive(
        self, 
        ids: int | str | Iterable[int | str], 
        /, 
        to_pid: int = 0, 
        *, 
        async_: Literal[True], 
    ) -> Awaitable[dict]:
        ...
    def receive(
        self, 
        ids: int | str | Iterable[int | str], 
        /, 
        to_pid: int = 0, 
        *, 
        async_: Literal[False, True] = False, 
    ) -> dict | Awaitable[dict]:
        """接收分享文件到网盘
        :param ids: 要转存到文件 id（这些 id 归属分享链接）
        :param to_pid: 你的网盘的一个目录 id（这个 id 归属你的网盘）
        """
        def gen_step():
            nonlocal ids
            if isinstance(ids, int):
                ids = str(ids)
            elif isinstance(ids, Iterable):
                ids = ",".join(map(str, ids))
            if not ids:
                raise ValueError("no id (to file) to receive")
            payload = {
                "share_code": self.share_code, 
                "receive_code": self.receive_code, 
                "file_id": ids, 
                "cid": to_pid, 
            }
            return (yield partial(
                self.client.share_receive, 
                payload, 
                request=self.async_request if async_ else self.request, 
                async_=async_, 
            ))
        return run_gen_step(gen_step, async_=async_)

    @overload
    def stat(
        self, 
        id_or_path: IDOrPathType = "", 
        /, 
        pid: None | int = None, 
        *, 
        async_: Literal[False] = False, 
    ) -> stat_result:
        ...
    @overload
    def stat(
        self, 
        id_or_path: IDOrPathType = "", 
        /, 
        pid: None | int = None, 
        *, 
        async_: Literal[True], 
    ) -> Awaitable[stat_result]:
        ...
    def stat(
        self, 
        id_or_path: IDOrPathType = "", 
        /, 
        pid: None | int = None, 
        *, 
        async_: Literal[False, True] = False, 
    ) -> stat_result | Awaitable[stat_result]:
        "检查路径的属性，就像 `os.stat`"
        def gen_step():
            attr = yield partial(self.attr, id_or_path, pid=pid, async_=async_)
            is_dir = attr["is_directory"]
            timestamp: float = attr["timestamp"]
            return stat_result((
                (S_IFDIR if is_dir else S_IFREG) | 0o444, # mode
                cast(int, attr["id"]), # ino
                cast(int, attr["parent_id"]), # dev
                1, # nlink
                self.user_id, # uid
                1, # gid
                cast(int, 0 if is_dir else attr["size"]), # size
                timestamp, # atime
                timestamp, # mtime
                timestamp, # ctime
            ))
        return run_gen_step(gen_step, async_=async_)

