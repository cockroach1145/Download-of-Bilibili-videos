import requests
import re
import os
import subprocess
import time

# -------- 配置 --------
SAVE_DIR = "./downloads"
VIDEO_QUALITY = 64   # 16=360P 32=480P 64=720P 80=1080P 112=1080P+ 116=1080P60 120=4K 125=HDR
USE_FFMPEG = True
SESSDATA = ""        # ← 填入你的 SESSDATA（1080P+/4K 必需）
SLEEP_BETWEEN = 2    # 每集间隔秒数，避免风控
# ----------------------

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

api_headers = {
    'user-agent': UA,
    'Referer': 'https://www.bilibili.com/',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9',
}
dl_headers = {
    'user-agent': UA,
    'Referer': 'https://www.bilibili.com/',
    'Origin': 'https://www.bilibili.com',
    'Accept': '*/*',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'Range': 'bytes=0-',
}
if SESSDATA:
    api_headers['Cookie'] = f'sessdata={SESSDATA}'
    dl_headers['Cookie'] = f'sessdata={SESSDATA}'

os.makedirs(SAVE_DIR, exist_ok=True)

# -------- 通用工具 --------
def safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', '_', name).strip() or 'untitled'

def is_fmp4_stream(b: bytes) -> bool:
    """fMP4 文件头：[4字节长度] + 'ftyp' + brand"""
    return bool(b) and len(b) >= 12 and b[4:8] == b'ftyp'

def safe_remove(*paths):
    for p in paths:
        if p and os.path.exists(p):
            try: os.remove(p)
            except OSError: pass

def cleanup_tmp():
    """清理上次失败残留的 .tmp_ 文件"""
    for f in os.listdir(SAVE_DIR):
        if f.startswith('.tmp_'):
            safe_remove(f"{SAVE_DIR}/{f}")

def dl(url, path):
    """下载单个流，校验是视频流而非 HTML 错误页"""
    r = requests.get(url, headers=dl_headers, stream=True, timeout=30)
    r.raise_for_status()
    ctype = r.headers.get('content-type', '').lower()
    if 'text' in ctype or 'json' in ctype or 'html' in ctype:
        sample = next(r.iter_content(512), b'').decode('utf-8', errors='ignore')
        raise RuntimeError(f"返回的不是视频流 (Content-Type={ctype})\n内容片段: {sample[:200]}")
    total = int(r.headers.get('content-length', 0))
    size, first = 0, None
    with open(path, 'wb') as f:
        for chunk in r.iter_content(1024 * 1024):
            if not chunk: continue
            if first is None:
                first = chunk
                if not is_fmp4_stream(first):
                    raise RuntimeError(f"下载内容不是视频流（疑似 HTML 反爬页）：\n{chunk[:200].decode('utf-8', errors='ignore')}")
            f.write(chunk)
            size += len(chunk)
            print(f"\r下载中 {size//1024//1024}MB / {total//1024//1024}MB", end='', flush=True)
    print()
    if size < 1024:
        raise RuntimeError(f"下载文件过小（{size} 字节），疑似错误页")

def ffmpeg_ok(args):
    """运行 ffmpeg，失败时把 stderr 抛出来"""
    try:
        subprocess.run(args, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffmpeg 失败: {e.stderr.decode('utf-8', errors='ignore')[:500]}") from e

# -------- B站 API 封装 --------
def api_get(url, params=None):
    """GET B站接口，统一校验 code，返回 data 字段"""
    r = requests.get(url, headers=api_headers, params=params, timeout=30)
    r.raise_for_status()
    j = r.json()
    if j.get('code') != 0:
        raise RuntimeError(f"B站API错误 {j.get('code')}: {j.get('message')}")
    return j.get('data')

def get_video_info(bvid: str) -> dict:
    """获取视频信息（含 ugc_season 合集字段）"""
    return api_get(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")

def fetch_all_archives(mid: int, season_id: int) -> list:
    """
    通过 seasons_archives_list 接口分页获取合集所有视频。
    返回 archives 列表（每项含 bvid/aid/title，但无 cid）。
    这是拿合集"完整视频列表"最可靠的方式——view 接口的 ugc_season.sections
    对分章节大合集只会返回当前 BV 所在 section 的 episodes，其他 section 为空。
    """
    archives = []
    page_num, page_size = 1, 30
    while True:
        data = api_get("https://api.bilibili.com/x/polymer/space/seasons_archives_list", params={
            'mid': mid, 'season_id': season_id,
            'page_num': page_num, 'page_size': page_size,
            'sort_reverse': False,
        })
        arcs = data.get('archives', [])
        archives.extend(arcs)
        total = data.get('page', {}).get('total', 0)
        if not arcs or len(archives) >= total:
            break
        page_num += 1
    return archives

def get_cid_by_bvid(bvid: str):
    """通过 pagelist 接口获取视频第一个分P的 cid"""
    data = api_get("https://api.bilibili.com/x/player/pagelist", params={'bvid': bvid})
    if not data:
        return None
    return data[0].get('cid')

def _pick_ep(ep: dict) -> dict:
    return {'bvid': ep.get('bvid', ''), 'cid': ep.get('cid'),
            'aid': ep.get('aid'), 'title': ep.get('title', ep.get('bvid', 'untitled'))}

def get_collection_episodes(bvid: str) -> list:
    """
    返回合集所有分集（每集含 bvid/cid/aid/title），若非合集则返回单元素。
    策略：
      1) 优先用 seasons_archives_list 拿合集全部 bvid（最完整），再用 pagelist 拿每集 cid
         —— 修复"多 section 大合集只下载每集合第一集"的问题
      2) 接口失败时回退到 view 的 ugc_season.sections[].episodes（小合集可用）
      3) 都失败则当作单视频
    """
    info = get_video_info(bvid)
    season = info.get('ugc_season')
    if not season:
        return [{'bvid': info['bvid'], 'cid': info['cid'],
                 'aid': info.get('aid'), 'title': info['title']}]

    owner_mid = info.get('owner', {}).get('mid')
    season_id = season.get('id')

    # 路径 1：seasons_archives_list（推荐，能拿到所有视频）
    if owner_mid and season_id:
        try:
            archives = fetch_all_archives(owner_mid, season_id)
            episodes = []
            for arc in archives:
                bv = arc.get('bvid')
                if not bv:
                    continue
                try:
                    cid = get_cid_by_bvid(bv)
                except Exception as e:
                    print(f"  ⚠️ 获取 {bv} 的 cid 失败: {e}")
                    cid = None
                if cid is None:
                    continue
                episodes.append({'bvid': bv, 'cid': cid,
                                 'aid': arc.get('aid'),
                                 'title': arc.get('title', bv)})
            if episodes:
                return episodes
            print("⚠️ seasons_archives_list 返回空，回退到 view 数据...")
        except Exception as e:
            print(f"⚠️ seasons_archives_list 失败，回退到 view 数据: {e}")

    # 路径 2：view 的 ugc_season.sections[].episodes（兜底）
    eps = []
    for sec in season.get('sections', []):
        for ep in sec.get('episodes', []):
            eps.append(_pick_ep(ep))
    for ep in season.get('episodes', []):
        eps.append(_pick_ep(ep))

    seen, dedup = set(), []
    for ep in eps:
        if ep['bvid'] and ep['bvid'] not in seen:
            seen.add(ep['bvid'])
            dedup.append(ep)
    return dedup or [{'bvid': info['bvid'], 'cid': info['cid'],
                      'aid': info.get('aid'), 'title': info['title']}]

def get_play_url(bvid: str, cid: int) -> dict:
    return api_get("https://api.bilibili.com/x/player/playurl", params={
        'bvid': bvid, 'cid': cid, 'qn': VIDEO_QUALITY,
        'fnval': 16, 'fnver': 0, 'fourk': 1, 'platform': 'pc'
    })

# -------- 下载单集 --------
def _download_stream(bvid: str, cid: int, tmp_suffix: str, out: str) -> str:
    """用给定 cid 调 playurl 下载并合并为一个 mp4。
    tmp_suffix 用于临时文件命名，避免分P 之间互相覆盖。
    """
    play = get_play_url(bvid, cid)
    dash = play.get('dash')

    if dash:
        # 视频流：取不超过目标清晰度的最高一档
        vlist = sorted(dash['video'], key=lambda x: x['id'], reverse=True)
        video = next((v for v in vlist if v['id'] <= VIDEO_QUALITY), vlist[0])
        # 音频流：取最高带宽
        alist = sorted(dash.get('audio', []), key=lambda x: x.get('bandwidth', 0), reverse=True)
        audio = alist[0] if alist else None

        v_tmp = f"{SAVE_DIR}/.tmp_{tmp_suffix}_v.m4s"
        a_tmp = f"{SAVE_DIR}/.tmp_{tmp_suffix}_a.m4s"

        print(f"  下载视频流 qn={video['id']}")
        dl(video['baseUrl'], v_tmp)

        got_audio = False
        if audio:
            try:
                print(f"  下载音频流")
                dl(audio['baseUrl'], a_tmp)
                got_audio = True
            except Exception as e:
                print(f"  ⚠️ 音频下载失败：{e}（将只输出视频）")
                safe_remove(a_tmp)

        if got_audio and USE_FFMPEG:
            ffmpeg_ok(['ffmpeg', '-y', '-i', v_tmp, '-i', a_tmp,
                       '-c:v', 'copy', '-c:a', 'copy', out])
            safe_remove(v_tmp, a_tmp)
        else:
            os.replace(v_tmp, out)
    else:
        # 旧 durl 格式（可能多段，用 ffmpeg concat 合并）
        durl = play['durl']
        if len(durl) == 1:
            dl(durl[0]['url'], out)
        else:
            segs = []
            for i, seg in enumerate(durl):
                p = f"{SAVE_DIR}/.tmp_{tmp_suffix}_seg_{i}.mp4"
                print(f"  下载分段 {i+1}/{len(durl)}")
                dl(seg['url'], p)
                segs.append(p)
            list_file = f"{SAVE_DIR}/.tmp_{tmp_suffix}_concat.txt"
            with open(list_file, 'w', encoding='utf-8') as f:
                for p in segs:
                    f.write(f"file '{os.path.basename(p)}'\n")
            ffmpeg_ok(['ffmpeg', '-y', '-f', 'concat', '-safe', '0',
                       '-i', list_file, '-c', 'copy', out])
            safe_remove(list_file, *segs)
    return out


def download_one_video(bvid: str, default_cid: int, title: str, idx: int, total: int,
                       season_title: str = None) -> str:
    """下载一个合集视频（可能含多个分P）。
    文件夹结构：
      - 合集中的分集: downloads/{season_title}/{idx:02d}_{title}/P{p:02d}_{part}.mp4
      - 单视频多分P:  downloads/{title}/P{p:02d}_{part}.mp4
      - 单视频单分P:  downloads/{title}.mp4（不建文件夹）
    临时文件统一放 SAVE_DIR 根目录，避免污染子文件夹。
    """
    # 拿该视频的所有分P
    pages = []
    try:
        pages = api_get("https://api.bilibili.com/x/player/pagelist",
                        params={'bvid': bvid}) or []
    except Exception as e:
        print(f"  ⚠️ pagelist 失败，用默认 cid 当作单分P: {e}")
    if not pages:
        pages = [{'cid': default_cid, 'part': title}]

    num_p = len(pages)
    if num_p > 1:
        print(f"  该视频共 {num_p} 个分P")

    # 决定输出目录
    if season_title and total > 1:
        # 合集中的分集：合集文件夹 + 分集子文件夹
        out_dir = f"{SAVE_DIR}/{safe_name(season_title)}/{idx:02d}_{safe_name(title)}"
    elif num_p > 1:
        # 单视频多分P：按视频标题建文件夹
        out_dir = f"{SAVE_DIR}/{safe_name(title)}"
    else:
        # 单视频单分P：直接放 SAVE_DIR
        out_dir = SAVE_DIR
    os.makedirs(out_dir, exist_ok=True)

    last_out = None
    for p_idx, page in enumerate(pages, 1):
        cid = page.get('cid')
        part = page.get('part') or title
        if num_p > 1:
            out = f"{out_dir}/P{p_idx:02d}_{safe_name(part)}.mp4"
        elif season_title and total > 1:
            out = f"{out_dir}/{idx:02d}_{safe_name(title)}.mp4"
        else:
            out = f"{out_dir}/{safe_name(title)}.mp4"

        if os.path.exists(out):
            print(f"  ✓ P{p_idx}/{num_p} 已存在，跳过")
            last_out = out
            continue

        print(f"  --- P{p_idx}/{num_p} {part} ---")
        # 临时文件唯一标识，避免分P 之间互相覆盖
        tmp_suffix = f"{idx}_{p_idx}" if total > 1 else f"p{p_idx}"
        last_out = _download_stream(bvid, cid, tmp_suffix, out)
    return last_out


def parse_choice(s: str, max_n: int) -> list:
    """解析用户输入的序号选择：'1,3' / '1-3' / '1,3-5'。返回去重升序的索引列表。"""
    result = []
    for part in s.replace('，', ',').split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            try:
                a, b = part.split('-', 1)
                a, b = int(a), int(b)
                if a > b:
                    a, b = b, a
                result.extend(range(a, b + 1))
            except ValueError:
                continue
        else:
            try:
                result.append(int(part))
            except ValueError:
                continue
    seen, dedup = set(), []
    for i in result:
        if 1 <= i <= max_n and i not in seen:
            seen.add(i)
            dedup.append(i)
    return dedup


# -------- 主流程 --------
def main():
    cleanup_tmp()
    raw = input("请输入B站视频链接（支持合集/单视频，多个合集用空格/逗号/换行分隔）: ").strip()
    found = re.findall(r'(BV[0-9A-Za-z]{10})', raw)
    if not found:
        raise SystemExit("无法从输入提取 BV 号")
    seen, bvids = set(), []
    for bv in found:
        if bv not in seen:
            seen.add(bv)
            bvids.append(bv)
    print(f"共检测到 {len(bvids)} 个 BV：{', '.join(bvids)}\n")

    collections = []
    for bi, bv in enumerate(bvids, 1):
        try:
            info = get_video_info(bv)
            season = info.get('ugc_season')
            episodes = get_collection_episodes(bv)
            season_title = season.get('title') if season else info.get('title', bv)
            is_collection = season is not None or len(episodes) > 1
            print(f"  [{bi}] [{bv}] {season_title}  ({len(episodes)} 集, {'合集' if is_collection else '单视频'})")
            
            # 打印合集视频列表并让用户选择
            if is_collection and len(episodes) > 1:
                print(f"    合集视频列表:")
                for i, ep in enumerate(episodes, 1):
                    print(f"      {i:02d}. {ep['title']}")
                choice = input(f"    请选择要下载的序号 (如 1,3 或 2-5，回车=全部，q=跳过此合集): ").strip().lower()
                if choice == 'q':
                    print(f"    已跳过 {season_title}")
                    continue
                if choice:
                    indices = parse_choice(choice, len(episodes))
                    if not indices:
                        print(f"    ⚠️ 无有效选择，跳过此合集")
                        continue
                    episodes = [episodes[i-1] for i in indices]
                    print(f"    已选择 {len(episodes)} 集")
            
            collections.append({
                'bvid': bv,
                'season_title': season_title,
                'episodes': episodes,
                'is_collection': is_collection,
            })
        except Exception as e:
            print(f"  ⚠️ 解析 {bv} 失败: {e}")

    if not collections:
        raise SystemExit("没有可下载的内容")

    if len(collections) > 1:
        print(f"\n共 {len(collections)} 个合集/视频已就绪，是否继续？(y/n): ", end='')
        if input().strip().lower() == 'n':
            # 再次选择要下载的合集
            print("请选择要下载的合集序号 (如 1,3 或 1-3，回车=全部):")
            choice = input("> ").strip().lower()
            if choice:
                indices = parse_choice(choice, len(collections))
                collections = [collections[i-1] for i in indices]
            if not collections:
                raise SystemExit("未选择任何合集")

    total_col = len(collections)
    total_ok, total_fail = 0, 0
    for ci, coll in enumerate(collections, 1):
        eps = coll['episodes']
        n = len(eps)
        print(f"\n========== [{ci}/{total_col}] {coll['season_title']} (共 {n} 集) ==========")
        season_title = coll['season_title'] if coll['is_collection'] else None

        for i, ep in enumerate(eps, 1):
            print(f"\n=== [{i}/{n}] {ep['title']} ===")
            try:
                out = download_one_video(ep['bvid'], ep['cid'], ep['title'], i, n,
                                         season_title=season_title)
                print(f"  ✓ 完成: {out}")
                total_ok += 1
            except Exception as e:
                print(f"  ✗ 失败: {e}")
                total_fail += 1
            if i < n:
                time.sleep(SLEEP_BETWEEN)

    print("\n========= 汇总 =========")
    print(f"  成功: {total_ok}  失败: {total_fail}")


if __name__ == '__main__':
    main()