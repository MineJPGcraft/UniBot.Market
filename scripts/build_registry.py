#!/usr/bin/env python3
"""构建 UniBot 扩展市场注册表。

读取 `extensions/*.json` 元数据（用户通过 PR 提交），从各扩展仓库的
GitHub Release 拉取扩展包 zip 并计算 SHA-256，生成 UniBot 端消费的
`extensions.json`。

SHA-256 由本脚本（仓库维护者控制的 workflow）计算并写入，用户提交的
元数据中不包含也不接受 sha256 字段，防止篡改。

用法：
    python scripts/build_registry.py             # 构建并写入 extensions.json
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


def build_extension(meta: dict, strict: bool) -> dict | None:
    """构建单个扩展的注册表条目；strict 模式下失败抛异常。"""
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

    releases = []
    for release in data:
        tag = release.get('tag_name', '')
        version = tag[1:] if tag.startswith('v') else tag
        if not version:
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
    return meta


def main() -> None:
    parser = argparse.ArgumentParser(description='构建 UniBot 扩展市场注册表')
    parser.add_argument('--validate', action='store_true', help='严格校验模式（PR 用），不写文件')
    args = parser.parse_args()

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
        entry = build_extension(meta, strict=args.validate)
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