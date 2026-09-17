# Disk Manager

ブラウザからディスク操作を行える Web インターフェースです。
ddrescueGUI の機能はそのままに、UI をサイドバー切替式に作り直したものです。

- ツール名: **Disk Manager**
- バージョン: **v.0.0.3**
- 動作環境: Ubuntu / Debian / CachyOS（Arch 系含む）（systemd を使用）
- デフォルトポート: **3361**（`127.0.0.1` のみにバインド。LAN には非公開）
- 公開方法: Tailscale Serve による HTTPS 化を想定（Tailnet 内のみ公開）
- 使用ツール: `ddrescue`（Debian では `gddrescue`）, `smartmontools`, `lsblk` / `blkid`（Arch 系では `util-linux`）, `file`, `git`, `clonezilla`, `partclone`, `rsync`, `parted`, `dosfstools`, `ntfs-3g`, `exfatprogs`, `xfsprogs`, `btrfs-progs`, `e2fsprogs`
- ファビコンは ddrescueGUI と同じものを使用しています。

## 画面構成

起動時のメイン画面は「パーティション操作」で、右ペインに内容を表示します。
サイドバーの各機能名をクリックすると右ペインが切り替わります。

上からの並び順:

1. パーティション操作（`partition.html`）
2. S.M.A.R.T.情報（`smart.html`）
3. Clonezilla（`clone.html`）
4. ddrescue（`rescue.html`）
5. rsync（`rsync.html`）
6. ディスク完全消去（`wipe.html`）
7. アップデート（システムページ）
8. 再起動（システムページ）
9. リフレッシュ（システムページ）

各機能ペインは `iframe` で読み込まれ、直接 URL を開いた場合はトップ（`/#<機能名>`）へ誘導されます。
アップデート／再起動／リフレッシュは右ペインのシステムカードから実行します。

## インストール

root 権限で実行します。スクリプトは GitHub から最新版をダウンロード（または更新）し、systemd サービスとして登録します。

```bash
sudo wget -O /tmp/diskmanager-install.sh \
  https://raw.githubusercontent.com/hirogura/diskmanager/main/install.sh
sudo bash /tmp/diskmanager-install.sh
```

インストール完了後、ブラウザで以下にアクセスします。

```
http://localhost:3361
```

サーバーは `127.0.0.1` のみにバインドされるため、LAN からの直接アクセスはできません。
別の PC からアクセスするには、下記の Tailscale Serve による HTTPS 公開を利用してください。

`install.sh` は /opt/diskmanager へ自動的にインストールします。
OS を自動判定し、Debian / Ubuntu 系では `apt-get`、CachyOS / Arch 系では `pacman` で依存パッケージを導入します。

| ディストリビューション | パッケージマネージャ | 導入パッケージ |
| --- | --- | --- |
| Debian / Ubuntu | `apt-get` | `python3`, `gddrescue`, `smartmontools`, `fdisk`, `git`, `clonezilla`, `partclone`, `rsync`, `parted`, `dosfstools`, `ntfs-3g`, `exfatprogs`, `xfsprogs`, `btrfs-progs`, `e2fsprogs` |
| CachyOS / Arch 系 | `pacman` | `python`, `ddrescue`, `smartmontools`, `util-linux`, `file`, `git`, `clonezilla`, `partclone`, `rsync`, `parted`, `dosfstools`, `ntfs-3g`, `exfatprogs`, `xfsprogs`, `btrfs-progs`, `e2fsprogs` |

Tailscale が導入済みの環境では、インストーラが自動で Tailscale Serve を設定し、
Tailnet 内のみ HTTPS（`https://<マシン名>.<tailnet>.ts.net:3361`）で公開します。
手動で設定する場合は以下を実行してください。

```bash
sudo tailscale serve --bg --https=3361 http://127.0.0.1:3361
```

公開範囲は Tailnet 内のみです。LAN 内への公開は行いません。

## 使用方法

1. ブラウザで `http://localhost:3361`（または Tailnet の HTTPS URL）を開きます。
2. 左サイドバーから使いたい機能を選びます。
3. 各機能の操作方法は ddrescueGUI と同じです。

### アップデート

サイドバーの「アップデート」から実行します。
GitHub の `hirogura/diskmanager`（`main` ブランチ）から最新版を取得し、インストーラーがサービスを再起動します。
実行中のタスク（レスキュー／クローン／消去など）がある場合は中断してから実行してください。

```bash
sudo bash /tmp/diskmanager-install.sh
```

上記のようにインストールスクリプトを再実行して更新することもできます。

### 再起動

サイドバーの「再起動」から Disk Manager サービスを再起動します。
実行中のタスクがある場合は中断してから実行してください。

```bash
sudo systemctl restart diskmanager
```

### リフレッシュ

サイドバーの「リフレッシュ」から画面全体を再読み込みします。

### サービス管理

```bash
sudo systemctl status diskmanager   # 状態確認
sudo systemctl restart diskmanager  # 再起動
sudo journalctl -u diskmanager -f   # ログ表示
```

## アンインストール

サービスを停止・無効化し、設定ファイルとインストール先を削除します。

```bash
sudo systemctl stop diskmanager
sudo systemctl disable diskmanager
sudo rm /etc/systemd/system/diskmanager.service
sudo systemctl daemon-reload
sudo rm -rf /opt/diskmanager
```

Tailscale Serve を設定していた場合は、公開設定も解除します。

```bash
sudo tailscale serve --https=3361 off
```

ログ（実行履歴・マップファイル）も削除されます。保存したい場合は削除前にバックアップしてください。

## ディレクトリ構成

```
 /opt/diskmanager/
 ├── server.py            # Web サーバー本体（ポート 3361、バージョン 0.0.3）
 ├── public/index.html    # サイドバー＋右ペインのシェル
 ├── public/app.js        # ページ切替・アップデート／再起動の待機処理
 ├── public/app.css       # シェル用スタイル
 ├── public/pane.js       # 各機能ページ共通（iframe 判定・直接アクセス時の誘導）
 ├── public/pane.css      # 各機能ページ共通スタイル
 ├── public/partition.html # パーティション操作 UI
 ├── public/smart.html    # S.M.A.R.T.情報 UI
 ├── public/clone.html    # Clonezilla 高速クローン UI
 ├── public/rescue.html   # ddrescue レスキュー UI
 ├── public/rsync.html    # rsync ファイルコピー UI
 ├── public/wipe.html     # ディスク完全消去 UI
 ├── public/favicon.svg   # ファビコン（ddrescueGUI と同じ）
 ├── logs/                # 実行ログ・マップファイル（自動生成）
 └── install.sh           # インストーラ
```

## 機能概要

- パーティション操作（グラフィカル表示。サイズ拡大／縮小、作成、削除。マウント中・システムドライブは保護）
- S.M.A.R.T.情報表示
- Clonezilla 高速クローン（正常ディスク向け・使用中セクタのみ複製。システムドライブは対象外。進捗グラフ＋残り時間表示、マウント時はアンマウント確認あり。開始前にコピー元のNTFS健全性を確認し、破損時は開始せず修復方法を案内）
- ddrescue レスキュー（全セクタ複製・障害ディスク対応。進行状況のリアルタイム表示、ログ管理）
- rsync ファイルコピー（ドライブ／ddrescueの.imgイメージからマウントし、フォルダ・ファイルを選択してコピー。-r/-t/-u 切替、コピー先フォルダ自動作成）
- ディスク完全消去（ゼロ／乱数上書き、SSD は Sanitize 相当を自動選択）

## 注意事項

- 復旧・消去対象のディスクを誤指定しないよう、実行前にデバイスのサイズ・モデル・シリアル番号を必ず確認してください。
- 実行ログ（`logs/`）にはデバイス情報が含まれるため、リポジトリには公開されません（`.gitignore` で除外）。

## ライセンス

MIT License です。詳細は [LICENSE](./LICENSE) を参照してください。
