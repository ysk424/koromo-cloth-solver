# Yarn-level Knitwear Solver：学生向け概要 / Student Overview

## OpenMPとパス / OpenMP and paths

配布ZIPをBlenderで使うだけなら、OpenMPのインストールやパス設定は不要です。
Windows版DLLにはOpenMPランタイムを静的リンクしてあるため、Extensionは同梱の
`yarn_level_knitware_solver.dll`だけを読み込みます。`OMP_NUM_THREADS`などの環境変数も
必須ではありません。UIの「スレッド数」を0にすると、OpenMPが利用できる最大数を
自動選択します。

ソースからビルドする場合は、OpenMP対応のC++コンパイラが必要です。CMakeが
`find_package(OpenMP)`で通常は自動検出するので、OpenMPライブラリのパスを手入力する
必要はありません。また、現在のビルドは`omp-contact-solver`のソースを使用するため、
既定では本リポジトリと`omp-contact-solver`を同じ親フォルダーに置きます。別の場所に
置く場合だけ、`-DOCS_ROOT=C:/path/to/omp-contact-solver`を指定します。このパスは
ビルド時専用であり、完成したDLLやBlenderでの実行時には不要です。

For normal Blender use, neither an OpenMP installation nor an OpenMP path is
required. The Windows DLL statically contains its OpenMP runtime. OpenMP is
required only when compiling the project, and CMake normally detects it from
the selected compiler. The `OCS_ROOT` path points to the `omp-contact-solver`
source during compilation; it is not a runtime dependency.

## 日本語解説（約1000字）

Yarn-level Knitwear Solverは、Blender上の服を人体の動きに沿って変形させ、その結果を
アニメーションとして保存するCPUベースの布シミュレーターです。名前には「糸レベル」
とありますが、現段階では一本一本の糸を棒として計算するのではなく、服を三角形の
集合からなる薄い弾性シート、つまり連続体シェルとして扱います。網目を、質点とばねが
多数つながった模型として想像すると近いですが、実際には三角形の伸び、曲げ、面積や
方向の変化をProjective DynamicsとADMMという反復法で同時に調整します。

入力は服のSHELLメッシュと、衝突相手となるBODYメッシュです。準備操作では元データを
直接変更せず、服のスナップショットとボディの複製を作ります。ボディ側のArmatureなどの
変形モディファイアは維持されるため、各フレームでアニメーション後の頂点位置を取得
できます。ただし、計算中に頂点数や面の接続関係が変わらないことが必要です。必要なら
ボディをワールドZ=0.40～1.45 mの範囲に切り抜き、計算量を減らします。

各フレームでは、まず重力、慣性、速度減衰から服の移動先を予測します。次に、伸びと
曲げの制約、最大主伸びの制限、縫合糸、服頂点とボディ三角形の接触を一つの方程式系に
まとめ、PCG法で繰り返し解きます。ボディ三角形はBVHという空間探索木に格納されるため、
全三角形を毎回調べるより高速です。`yohsai_zozo_stitch`というBoolean EDGE属性があれば、
マークされた辺の両端を縫合ペアとして使います。縫合は頂点を結合するのではなく、有限の
強さを持つ制約なので、パネルのUVや材質境界を保てます。重い頂点・三角形処理はOpenMPで
CPUの複数コアへ分配されます。

計算結果は準備済みの服へ絶対Shape Keyとしてフレームごとに保存され、元の服とボディは
保持されます。この方式は結果をタイムラインで再生しやすい一方、長いベイクではShape Key
数とファイル容量が増えます。現在は服の自己衝突、辺同士の衝突、厳密なCCD、固定点、糸の
ねじりや滑りを実装していません。そのため、論文の完全再現ではなく、アニメーションする
人体、縫い合わせ、伸び制限を扱える実用的な連続体布ソルバーという位置付けです。

## English explanation (about 1000 characters)

Yarn-level Knitwear Solver is a CPU cloth simulator that deforms a garment
against an animated Blender body and bakes the result. Despite its name, it does
not yet model each yarn as a rod. The garment is a thin triangular elastic shell.
A spring net is a useful analogy, but Projective Dynamics, ADMM, and PCG solve
stretch, bending, strain limits, seams, and contact together.

The inputs are a garment `SHELL` and collision `BODY`. Preparation copies both,
preserving the originals. Body modifiers such as Armature are evaluated every
frame, but vertex count and topology must stay fixed. A world-Z crop can reduce
the collision mesh.

Each frame predicts motion from gravity, inertia, and damping, then solves the
constraints. A BVH speeds triangle searches and OpenMP uses multiple CPU cores.
Marked `yohsai_zozo_stitch` edges form finite-strength seams without merging
vertices, preserving UVs and material boundaries.

Results become absolute Shape Keys for easy playback. Self-collision, edge
contact, exact CCD, pins, yarn rods, twisting, and sliding are not implemented.
It is a practical continuum-cloth baseline, not a complete reproduction of the
referenced yarn-level paper.
