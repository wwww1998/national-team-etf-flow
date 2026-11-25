# -*- coding: utf-8 -*-
"""推送工具: 优先 git push; 若 github.com:443 被 DNS 污染/git 通道不可用, 自动改用 GitHub Git Data API 推送.

用法:
    python push.py                # 推送当前分支全部未推送的改动
    python push.py 文件1 文件2 ...  # 只推送指定文件(推荐, 避免覆盖 CI 刚更新的数据)

原理: 本机 github.com 常被解析到劫持 IP 导致 git 报 Connection reset, 而 api.github.com 正常,
      故以 API 覆盖/新增指定路径的 blob, 基于远端最新 tree 建新 commit 并快进 ref。
"""
import base64, json, os, re, subprocess, sys, time, requests

BASE = os.path.dirname(os.path.abspath(__file__))
BRANCH = "main"
SKIP = 50 * 1024 * 1024      # 单文件 >50MB 不上传(API blob 限制)


def git(*args, check=False):
    r = subprocess.run(["git"] + list(args), cwd=BASE, text=True, capture_output=True)
    if check and r.returncode:
        raise SystemExit("git %s 失败: %s" % (" ".join(args), r.stderr.strip()[:200]))
    return r


def api_push(files, msg):
    url = git("remote", "get-url", "origin", check=True).stdout.strip()
    m = re.search(r"x-access-token:([^@]+)@", url) or re.search(r"https://([^@]+)@", url)
    tok = os.environ.get("GITHUB_TOKEN") or (m.group(1) if m else "")
    if not tok:
        raise SystemExit("未找到 GitHub token")
    owner_repo = re.search(r"github\.com[/:]([^/]+/[^/.]+?)(?:\.git)?$", url).group(1)
    API = "https://api.github.com/repos/" + owner_repo
    H = {"Authorization": "token " + tok, "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"}

    def chk(r, what):
        if r.status_code >= 300:
            raise SystemExit("API %s 失败 %s: %s" % (what, r.status_code, r.text[:200]))
        return r.json()

    base = chk(requests.get(API + "/git/ref/heads/" + BRANCH, headers=H, timeout=60), "ref")["object"]["sha"]
    base_tree = chk(requests.get(API + "/git/commits/" + base, headers=H, timeout=60), "commit")["tree"]["sha"]
    print("远端 %s = %s" % (BRANCH, base[:8]))
    items, del_paths = [], []
    for p in files:
        full = os.path.join(BASE, p)
        rel = p.replace("\\", "/")
        if not os.path.exists(full):
            del_paths.append(rel)
            continue
        raw = open(full, "rb").read()
        if len(raw) > SKIP:
            print("  跳过超大文件", p)
            continue
        b = chk(requests.post(API + "/git/blobs", headers=H, timeout=300,
                              json={"content": base64.b64encode(raw).decode(), "encoding": "base64"}), "blob " + p)
        items.append({"path": rel, "mode": "100644", "type": "blob", "sha": b["sha"]})
        print("  %-38s %7.0fKB" % (p, len(raw) / 1024))
    for rel in del_paths:
        items.append({"path": rel, "mode": "100644", "type": "blob", "sha": None})
        print("  删除", rel)
    if not items:
        print("无可推送内容")
        return
    tree = chk(requests.post(API + "/git/trees", headers=H, timeout=180,
                             json={"base_tree": base_tree, "tree": items}), "tree")
    cm = chk(requests.post(API + "/git/commits", headers=H, timeout=180,
                           json={"message": msg, "tree": tree["sha"], "parents": [base]}), "commit")
    for _ in range(3):
        r = requests.patch(API + "/git/refs/heads/" + BRANCH, headers=H, timeout=60, json={"sha": cm["sha"]})
        if r.status_code < 300:
            print("API 推送成功: %s -> %s (%d 个文件)" % (base[:8], cm["sha"][:8], len(items)))
            return
        time.sleep(3)
    raise SystemExit("ref 更新失败: " + r.text[:200])


def main():
    files = sys.argv[1:]
    if not files:
        d = git("diff", "--name-only", "origin/" + BRANCH + "...HEAD")
        files = [x for x in d.stdout.splitlines() if x.strip()]
    msg = git("log", "-1", "--pretty=%s").stdout.strip() or "chore: 自动更新"
    print("尝试 git push ...", flush=True)
    r = git("push", "origin", BRANCH)
    if r.returncode == 0:
        print("git push 成功")
        return
    print("git 通道不可用(%s), 切换 API 推送" % (r.stderr.strip().splitlines() or ["unknown"])[-1][:90])
    api_push(files, msg)


if __name__ == "__main__":
    main()
