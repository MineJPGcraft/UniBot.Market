#!/usr/bin/env python3
"""构建 UniBot 扩展市场注册表。

读取 `extensions/*.json` 元数据（用户通过 PR 提交），从各扩展仓库的
GitHub Release 拉取扩展包 zip 并计算 SHA-256，生成 UniBot 端消费的
`extensions.json`。

SHA-256 由本脚本（仓库维护者控制的 workflow）计算并写入，用户提交的
元数据中不包含也不接受 sha256 字段，防止篡改。

用法：
    python scripts/build_registry.py             # 构建并写入 extensions.json（增量更新）
    python scripts/build_registry.py --force     # 强制重新下载所有扩展的所有版本
    python scripts/build_registry.py --force Placeholder      # 仅强制覆盖指定扩展
    python scripts/build_registry.py --force Placeholder A B  # 强制覆盖多个扩展
    python scripts/build_registry.py --validate  # 严格校验（PR 用），不写文件
"""

import argparse
import hashlib
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parent.parent
META_DIR = ROOT / 'extensions'
OUTPUT = ROOT / 'extensions.json'
API_BASE = 'https://api.github.com'
USER_AGENT = 'UniBot-Market-Builder'
# 每个扩展最多收录的版本数（GitHub API 单页上限）
MAX_RELEASES = 20

TOKEN = os.environ.get('GITHUB_TOKEN', '')


def api_get(url: str) -> object:
    """GET GitHub API 并返回解析后的 JSON。"""
    request = urllib.request.Request(url)
    request.add_header('Accept', 'application/vnd.github+json')
    request.add_header('User-Agent', USER_AGENT)
    if TOKEN:
        request.add_header('Authorization', f'Bearer {TOKEN}')
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode('utf-8'))


def download(url: str) -> bytes:
    """下载文件并返回字节内容。"""
    request = urllib.request.Request(url)
    request.add_header('User-Agent', USER_AGENT)
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def parse_version(version: str) -> tuple[int, ...] | None:
    """解析版本号为可排序元组（x.y.z），失败返回 None。"""
    match = re.match(r'^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?', version)
    if not match:
        return None
    return tuple(int(part) if part else 0 for part in match.groups())


def pick_asset(zip_assets: list[dict], extension_id: str, version: str) -> dict:
    """选择 zip 资产：优先精确匹配 <id>-<version>.zip，其次 <id>-*.zip，最后取第一个。"""
    exact = re.compile(rf'^{re.escape(extension_id)}[-_]{re.escape(version)}\.zip$', re.IGNORECASE)
    loose = re.compile(rf'^{re.escape(extension_id)}[-_].*\.zip$', re.IGNORECASE)
    for asset in zip_assets:
        if exact.match(asset.get('name', '')):
            return asset
    for asset in zip_assets:
        if loose.match(asset.get('name', '')):
            return asset
    return zip_assets[0]


def read_manifest_unibot(zip_data: bytes) -> str:
    """从扩展包 zip 中读取 Extension.toml 的 [compatibility].unibot，失败返回 '*'。"""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_data)) as archive:
            names = [name for name in archive.namelist() if name.endswith('Extension.toml')]
            if not names:
                return '*'
            names.sort(key=lambda name: name.count('/'))
            content = archive.read(names[0]).decode('utf-8')
            data = tomllib.loads(content)
            return str(data.get('compatibility', {}).get('unibot', '*'))
    except Exception:
        return '*'


def build_extension(meta: dict, strict: bool, existing: dict | None, force: bool) -> dict | None:
    """构建单个扩展的注册表条目；strict 模式下失败抛异常。

    existing 为注册表中该扩展的旧条目（增量模式复用其已存在的版本，避免重复下载）。
    force 为 True 时该扩展所有版本强制重新下载（覆盖已存在的）。
    """
    extension_id = meta['id']
    repo = meta['repo']
    try:
        data = api_get(f'{API_BASE}/repos/{repo}/releases?per_page={MAX_RELEASES}')
    except urllib.error.HTTPError as error:
        message = f'扩展 {extension_id}：获取 {repo} 的 Release 失败（HTTP {error.code}）'
        if strict:
            raise RuntimeError(message) from error
        print(f'⚠️ {message}，已跳过')
        return None
    except Exception as error:
        message = f'扩展 {extension_id}：获取 {repo} 的 Release 失败：{error}'
        if strict:
            raise RuntimeError(message) from error
        print(f'⚠️ {message}，已跳过')
        return None
    if not isinstance(data, list):
        message = f'扩展 {extension_id}：{repo} 的 Release 响应异常'
        if strict:
            raise RuntimeError(message)
        print(f'⚠️ {message}，已跳过')
        return None

    # 旧条目中仍存在的版本（GitHub 上依然存在）视为已收录，增量模式下直接复用
    existing_releases = {}
    if existing and not force:
        seen_tags = {release.get('tag_name', '') for release in data}
        for old in existing.get('releases', []):
            version = old.get('version', '')
            tag = f'v{version}' if not version.startswith('v') else version
            if tag in seen_tags:
                existing_releases[version] = old

    releases = []
    for release in data:
        tag = release.get('tag_name', '')
        version = tag[1:] if tag.startswith('v') else tag
        if not version:
            continue
        # 增量模式：该版本已存在则直接复用，不重新下载
        if version in existing_releases:
            releases.append(existing_releases[version])
            continue
        zip_assets = [
            asset for asset in release.get('assets', []) if asset.get('name', '').lower().endswith('.zip')
        ]
        if not zip_assets:
            continue
        asset = pick_asset(zip_assets, extension_id, version)
        asset_url = asset.get('browser_download_url', '')
        if not asset_url:
            continue
        try:
            zip_data = download(asset_url)
        except Exception as error:
            message = f'扩展 {extension_id}：下载 {asset_url} 失败：{error}'
            if strict:
                raise RuntimeError(message) from error
            print(f'⚠️ {message}，已跳过该版本')
            continue
        releases.append(
            {
                'version': version,
                'asset_url': asset_url,
                'sha256': hashlib.sha256(zip_data).hexdigest(),
                'unibot_version': read_manifest_unibot(zip_data),
            }
        )

    if not releases:
        message = f'扩展 {extension_id}：{repo} 没有可用的 zip Release 资产'
        if strict:
            raise RuntimeError(message)
        print(f'⚠️ {message}，已跳过')
        return None

    # 按版本升序排列（UniBot 取 releases 最后一个为最新版）
    releases.sort(key=lambda item: parse_version(item['version']) or (0, 0, 0))
    return {
        'id': extension_id,
        'name': meta['name'],
        'repo': repo,
        'description': meta.get('description', ''),
        'official': meta.get('official', False),
        'releases': releases,
    }


def validate_metadata(path: Path) -> dict:
    """校验单个元数据文件，返回解析后的字典。"""
    try:
        meta = json.loads(path.read_text('utf-8'))
    except Exception as error:
        print(f'❌ {path.name} 不是合法 JSON：{error}')
        sys.exit(1)
    if not isinstance(meta, dict):
        print(f'❌ {path.name} 必须是 JSON 对象')
        sys.exit(1)
    extension_id = meta.get('id', '')
    if not re.match(r'^[A-Za-z0-9_]+$', extension_id):
        print(f'❌ {path.name}：id 缺失或非法（须为字母数字下划线，如 Example）')
        sys.exit(1)
    if not meta.get('name'):
        print(f'❌ {path.name}：缺少 name（显示名称）')
        sys.exit(1)
    repo = meta.get('repo', '')
    if not re.match(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$', repo):
        print(f'❌ {path.name}：repo 格式非法（须为 owner/repo，如 MineJPGcraft/Example）')
        sys.exit(1)
    official = meta.get('official', False)
    if not isinstance(official, bool):
        print(f'❌ {path.name}：official 必须是布尔值（true/false）')
        sys.exit(1)
    return meta


def main() -> None:
    parser = argparse.ArgumentParser(description='构建 UniBot 扩展市场注册表')
    parser.add_argument('--force', nargs='*', default=None, metavar='ID',
                        help='强制重新下载并覆盖：不带参数时覆盖全部扩展，可指定一个或多个扩展 id')
    parser.add_argument('--validate', action='store_true', help='严格校验模式（PR 用），不写文件')
    args = parser.parse_args()

    # force 为 None（未加）→ 全增量；为 []（加 --force）→ 全覆盖；为 [id,...] → 仅指定扩展覆盖
    force_ids = set(args.force) if args.force is not None else None
    FORCE_ALL = force_ids is not None and not force_ids

    # 读取旧注册表，供增量模式复用已存在的版本
    old_registry = {}
    if not args.validate and OUTPUT.exists():
        try:
            old_registry = {entry['id']: entry for entry in json.loads(OUTPUT.read_text('utf-8'))}
        except Exception as error:
            print(f'⚠️ 读取旧 {OUTPUT.name} 失败（视为无缓存）：{error}')

    # 跳过以下划线开头的文件（如 _EXAMPLE.json 模板）
    meta_files = sorted(path for path in META_DIR.glob('*.json') if not path.name.startswith('_'))
    if not meta_files:
        if args.validate:
            print('❌ extensions/ 目录下没有元数据文件')
            sys.exit(1)
        # 非校验模式：生成空注册表
        content = json.dumps([], ensure_ascii=False, indent=2) + '\n'
        temp = OUTPUT.with_suffix('.tmp')
        temp.write_text(content, encoding='utf-8')
        temp.replace(OUTPUT)
        print(f'✅ 已生成 {OUTPUT.relative_to(ROOT)}：0 个扩展')
        return

    metas = []
    seen_ids = set()
    for path in meta_files:
        meta = validate_metadata(path)
        if meta['id'] in seen_ids:
            print(f'❌ 扩展 id 重复：{meta["id"]}')
            sys.exit(1)
        seen_ids.add(meta['id'])
        metas.append(meta)

    registry = []
    for meta in metas:
        # 未指定 id 时全覆盖；指定了 id 则仅匹配到的扩展强制覆盖
        force = force_ids is None or meta['id'] in force_ids
        entry = build_extension(meta, strict=args.validate,
                                existing=old_registry.get(meta['id']), force=force)
        # 增量模式：GitHub 上已删除的 release 不在新列表中，自然会被剔除
        if entry is not None:
            registry.append(entry)
    registry.sort(key=lambda entry: entry['id'])

    if args.validate:
        print(f'✅ 校验通过：{len(registry)} 个扩展可构建')
        return

    content = json.dumps(registry, ensure_ascii=False, indent=2) + '\n'
    temp = OUTPUT.with_suffix('.tmp')
    temp.write_text(content, encoding='utf-8')
    temp.replace(OUTPUT)
    print(f'✅ 已生成 {OUTPUT.relative_to(ROOT)}：{len(registry)} 个扩展')


if __name__ == '__main__':
    main()