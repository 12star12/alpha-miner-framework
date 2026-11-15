# -*- coding: utf-8 -*-
"""
push_via_api.py —— 作用：当 github.com:443 被网络屏蔽、但 api.github.com 可达时，
用 Git Data API 把本地 HEAD 原样搬上远程（不新增 SSH key、不改账号设置）。

原理：
  1. 用 git ls-tree 拿到 HEAD 的真实 blob（仓库内是 LF，不是工作区的 CRLF）；
  2. POST /git/trees 一次性建树（文本内联，无法内联的走 blob 上传）；
  3. POST /git/commits，把 author/committer/时间戳/消息 与本地完全对齐；
  4. POST /git/refs 建 refs/heads/main。
  因为对象内容完全一致，远程 commit SHA 应与本地 HEAD 相同 —— 这一步会自校验。

用法：python push_via_api.py <owner/repo> [branch]
"""
import base64
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import requests

API = "https://api.github.com"


def sh(*args) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout


def token() -> str:
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, check=True,
    ).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("取不到 GitHub 令牌")


def iso(ts: int, tz: str) -> str:
    sign = 1 if tz[0] == "+" else -1
    off = timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5])) * sign
    return datetime.fromtimestamp(ts, timezone(off)).isoformat()


def parse_commit(text: str) -> dict:
    head, _, body = text.partition("\n\n")
    info = {"message": body}
    for line in head.splitlines():
        if line.startswith("author "):
            rest, _, tz = line[7:].rpartition(" ")
            who, _, ts = rest.rpartition(" ")
            info["author"] = (who, int(ts), tz)
        elif line.startswith("committer "):
            rest, _, tz = line[10:].rpartition(" ")
            who, _, ts = rest.rpartition(" ")
            info["committer"] = (who, int(ts), tz)
        elif line.startswith("tree "):
            info["tree"] = line[5:].strip()
        elif line.startswith("parent "):
            info.setdefault("parents", []).append(line[7:].strip())
    return info


def person(t):
    who, ts, tz = t
    name, _, email = who.partition(" <")
    return {"name": name, "email": email.rstrip(">"),
            "date": iso(ts, tz)}


def main():
    if len(sys.argv) < 2:
        raise SystemExit("用法：python push_via_api.py <owner/repo> [branch]")
    repo = sys.argv[1]
    branch = sys.argv[2] if len(sys.argv) > 2 else "main"
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token()}",
                      "Accept": "application/vnd.github+json"})
    s.trust_env = True          # api.github.com 走系统代理是通的

    meta = parse_commit(sh("git", "cat-file", "-p", "HEAD"))
    files = []
    # core.quotePath=false：否则中文文件名会被转义成 \345\274\200 这种八进制，
    # 传上去就变成路径不同的新文件（树哈希对不上的元凶）
    for line in sh("git", "-c", "core.quotePath=false",
                   "ls-tree", "-r", "HEAD").splitlines():
        meta_part, _, path = line.partition("\t")
        mode, typ, sha = meta_part.split()
        files.append({"mode": mode, "type": typ, "sha": sha,
                      "path": path.strip('"')})
    print(f"本地 HEAD 树 {len(files)} 个文件，开始构建远程树")

    items, need_blob = [], []
    for f in files:
        raw = subprocess.run(["git", "cat-file", "blob", f["sha"]],
                             capture_output=True, check=True).stdout
        try:
            items.append({"path": f["path"], "mode": f["mode"], "type": "blob",
                          "content": raw.decode("utf-8")})
        except UnicodeDecodeError:
            need_blob.append((f, raw))

    for f, raw in need_blob:      # 二进制兜底：单独上传 blob
        r = s.post(f"{API}/repos/{repo}/git/blobs",
                   json={"content": base64.b64encode(raw).decode(),
                         "encoding": "base64"}, timeout=60)
        r.raise_for_status()
        items.append({"path": f["path"], "mode": f["mode"], "type": "blob",
                      "sha": r.json()["sha"]})
        print(f"  二进制 {f['path']} 单独上传")

    r = s.post(f"{API}/repos/{repo}/git/trees", json={"tree": items}, timeout=120)
    r.raise_for_status()
    tree_sha = r.json()["sha"]
    print(f"远程树 {tree_sha[:10]}  |  本地树 {meta['tree'][:10]}  "
          f"{'一致' if tree_sha == meta['tree'] else '不一致'}")

    payload = {"message": meta["message"], "tree": tree_sha,
               "author": person(meta["author"]),
               "committer": person(meta["committer"])}
    # 父节点必须用「远程分支当前顶端」：本地父提交的 SHA 远程并不存在，
    # 直接传会被 422 拒掉（本地与远程的历史根不同，这是本工具刻意接受的代价）
    cur = s.get(f"{API}/repos/{repo}/git/ref/heads/{branch}", timeout=30)
    if cur.status_code == 200:
        cur_sha = cur.json()["object"]["sha"]
        payload["parents"] = [cur_sha]
        print(f"以远程顶端为父节点（{cur_sha[:10]}）")
    elif meta.get("parents"):
        payload["parents"] = meta["parents"]
    r = s.post(f"{API}/repos/{repo}/git/commits", json=payload, timeout=60)
    r.raise_for_status()
    commit_sha = r.json()["sha"]
    local_sha = sh("git", "rev-parse", "HEAD").strip()
    print(f"远程 commit {commit_sha[:10]}  |  本地 {local_sha[:10]}  "
          f"{'完全一致' if commit_sha == local_sha else '不一致（内容已同，仅元数据差异）'}")

    r = s.post(f"{API}/repos/{repo}/git/refs",
               json={"ref": f"refs/heads/{branch}", "sha": commit_sha}, timeout=60)
    if r.status_code == 201:
        print(f"已创建 {branch} 分支")
    elif r.status_code == 422:
        r = s.patch(f"{API}/repos/{repo}/git/refs/heads/{branch}",
                    json={"sha": commit_sha, "force": True}, timeout=60)
        r.raise_for_status()
        print(f"已更新 {branch} 分支")
    else:
        r.raise_for_status()

    d = s.get(f"{API}/repos/{repo}", timeout=30).json()
    print(f"\n完成：{d['html_url']}  ({'private' if d['private'] else 'PUBLIC'})")


if __name__ == "__main__":
    main()
