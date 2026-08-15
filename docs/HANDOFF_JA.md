# 開発引き継ぎメモ

更新日: 2026-08-15

## 現在の到達点

Windows x64 DLLとして、三角形メッシュの服を連続体SHELL、三角形メッシュの
ボディをSTATICコライダーとして扱える状態です。Blenderからは
`blender_bridge/native.py` を介して `ctypes` でロードします。

実装済みの流れは次のとおりです。

1. ボディ頂点・三角形を `set_body()` へ渡す。
2. 服頂点・三角形・材料値を `set_cloth()` へ渡す。
3. 必要なら縫合頂点ペアを `set_seams()` へ渡す。
4. `build()` で制約とボディBVHを構築する。
5. 各フレームで、同じトポロジーのボディ頂点を `update_body()` へ渡す。
6. `step(dt)` の返り値を、服のシミュレーション用複製へ書き戻す。
7. `stats()` で接触数、反復数、最大主伸びなどを取得する。

Blender Extensionも実装済みです。元オブジェクトとは別にシミュレーション用
コピーを作り、アニメーションbodyを各フレームで評価し、結果を絶対Shape Keyへ
Bakeします。Boolean EDGE属性 `yohsai_zozo_stitch` がある場合は、その辺の両端を
明示的な縫合ペアとしてDLLへ渡します。

Extension 0.3.0以降ではBlenderのインターフェイス言語設定に追従する英語／日本語UIを
追加しました。Bake中はサイドバーの進捗バーとBlender下部のステータス領域に、
現在フレーム、終了フレーム、完了率を表示します。

元のBlenderオブジェクトを直接変更せず、シミュレーション用コピーまたは
Shape Keyへ結果を書く前提です。DLL境界では全頂点を同じ座標空間にそろえます。
現ブリッジとデモはBlenderのワールド座標（Z-up）を使用します。

## 重要な仕様判定

以下は要求に一致しています。

- 布を糸の集合ではなく、三角形の連続体SHELLとして扱う。
- DLLをBlender Extension/Pythonからロードできる。
- 服とボディコライダーを別メッシュとして渡せる。
- ボディの頂点変形をフレームごとに更新できる。
- ボディの頂点数と三角形トポロジーはシミュレーション中固定する。
- 重力はBlenderに合わせて `(0, 0, -9.81)` を既定とする。

以下は未達または意図的な制限です。

- SIGGRAPH 2026論文のNested Douglas--Rachford Splitting完全再現ではない。
- 現コアはProjective Dynamics/ADMMベースの連続体布ソルバーである。
- yarn-levelの離散ロッド、糸のねじり、糸同士の滑りは扱わない。
- 服の自己衝突、edge-edge接触、厳密CCDは未実装。
- 明示的な縫合糸制約は実装済み。PIN制約は未実装。
- 接触は主に服頂点対ボディ三角形である。

このため、現成果物は「Blenderとのデータ境界と布／ボディ接触を確認する
実用ベースライン」です。論文アルゴリズムと同一であるとは表示しません。

## ビルド

隣接する `../omp-contact-solver` の検証済みネイティブソースをコンパイルします。
生成DLLにはそのコアが組み込まれるため、実行時に
`omp_contact_solver.dll` は不要です。ただし、ソースからのビルド時には隣接
リポジトリが必要です。

```powershell
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build
ctest --test-dir build --output-on-failure
```

成果物:

```text
build/yarn_level_knitware_solver.dll
build/blender_bridge/native.py
build/packages/yarn_level_knitware_solver-0.3.1-windows-x64.zip
```

本プロジェクトとBlender ExtensionはGNU GPL version 3 or laterです。
`omp-contact-solver`由来のMITコード、GCC Runtime Library Exception、
MinGW-w64 runtimeの表示は`THIRD_PARTY_NOTICES.md`に保持します。

2026-08-15時点では、C ABI／コアテスト2/2とBlender 5.2 Extensionの24フレーム
バックグラウンド試験が成功しています。Extension試験は明示縫合属性、bodyの
アニメーション、40--145cmクロップ、ワールド／ローカル座標変換、絶対Shape Key
Bake、所有Bakeの削除まで確認します。

## CLOTHES_001_ZOZO 実データ確認

MCPで `Lumi-1-midori-2.blend` の次のデータを確認しました。

- 服: `CLOTHES_001_ZOZO_CLOTH`、6,563頂点、12,464面
- body: `CLOTHES_001_ZOZO_BODY`、Armatureモディファイア付き
- 服は2,537／2,384／821／821頂点の4パネル
- `yohsai_zozo_stitch` は220本すべて面に属さない縫合辺
- bodyは40--145cmクロップ後、121,746頂点／121,588面
- クロップ後bodyの評価トポロジーはフレーム1、40、145、250で同一

実シーンの3フレームスモークBakeも成功しました。220本の縫合について、最終
フレームの平均絶対誤差は約0.016mm、最大絶対誤差は約0.64mmでした。最終接触数は
8,890、最大変位は約2.13cmです。これは短い実データ経路確認であり、全250フレーム
の品質確定Bakeではありません。5%の伸び目標に対して観測最大主伸びは約10.27%の
ため、長時間Bakeでは設定と収束を引き続き確認します。

## Blender MCP可視化

Blender MCPブリッジのポートは `9876` を使用しました。

`tools/blender_mcp_visualize.py` をBlender内で実行すると、既存シーンを変更せず、
専用の `YLKS Solver Demo` シーンと `YLKS_DLL_DEMO` コレクションを作ります。

作成物:

- `YLKS_Body_Collider`: 卵型STATICコライダー
- `YLKS_Cloth_Input_Wire`: 落下前の正方形布。通常は非表示
- `YLKS_Cloth_Result`: DLLで計算した布
- デモ専用の床、カメラ、ライト

確認済みデモ値:

- ボディ: 922頂点、1,840三角形
- 布: 1,089頂点、2,048三角形
- 42フレーム、60 fps相当
- 累計接触投影: 47,858
- 最終接触統計: 3,391
- 最大主伸び: 約1.102

布が卵型の頂部を覆い、四辺が側面へ垂れるところまで確認しました。レンダーは
`build/ylks_demo_final.png` に生成されます。`build` はGit管理対象外です。

## 次回以降の優先候補

1. `CLOTHES_001_ZOZO`を全250フレームBakeし、縫合・伸び・接触を品質調整する。
2. 実キャラクターの高密度body用に、非表示の低密度コライダーを自動生成する。
3. PIN制約を追加し、肩やウエストなどを保持できるようにする。
4. 服の自己衝突を追加する。
5. Mesh Sequence Cache出力をShape Keyの代替として追加する。
6. 論文本文・補足資料から局所proxとmetric selectionを確定し、Nested DRSを
   別バックエンドとして実装する。

最初に進めるなら1です。実データの全区間で数値を確定してから、低密度body生成と
PIN／自己衝突へ進むのが自然です。
