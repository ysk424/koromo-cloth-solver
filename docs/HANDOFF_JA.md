# 開発引き継ぎメモ

更新日: 2026-08-14

## 現在の到達点

Windows x64 DLLとして、三角形メッシュの服を連続体SHELL、三角形メッシュの
ボディをSTATICコライダーとして扱える状態です。Blenderからは
`blender_bridge/native.py` を介して `ctypes` でロードします。

実装済みの流れは次のとおりです。

1. ボディ頂点・三角形を `set_body()` へ渡す。
2. 服頂点・三角形・材料値を `set_cloth()` へ渡す。
3. `build()` で制約とボディBVHを構築する。
4. 各フレームで、同じトポロジーのボディ頂点を `update_body()` へ渡す。
5. `step(dt)` の返り値を、服のシミュレーション用複製へ書き戻す。
6. `stats()` で接触数、反復数、最大主伸びなどを取得する。

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
- 服のPIN制約は未実装。
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
```

2026-08-14時点で、C ABIテストは1/1成功しています。PythonからDLLをロードし、
ボディ、服、`build`、`step`、`stats`を呼ぶスモークテストも成功しています。

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

## 明日以降の優先候補

1. Blender ExtensionのOperator/Panelを作り、服・ボディ選択とBakeをUI化する。
2. ワールド座標から出力先オブジェクトのローカル座標へ戻す処理をExtensionに置く。
3. Shape KeyまたはMesh Sequence Cacheへフレーム結果をベイクする。
4. 実キャラクターの高密度ボディ用に、非表示の低密度コライダーを自動生成する。
5. PIN制約を追加し、肩やウエストなどを保持できるようにする。
6. 服の自己衝突を追加する。
7. 論文本文・補足資料から局所proxとmetric selectionを確定し、Nested DRSを
   別バックエンドとして実装する。

最初に進めるなら、1〜3をまとめて行うのが自然です。これで現在の手動MCPデモが
通常のBlenderベイクワークフローになります。

