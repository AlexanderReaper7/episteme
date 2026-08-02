# Three arms, interleaved, so platform and CUDA toolkit are separable:
#
#   host-cuda13.3   b9882, CUDA 13.3  <- the installed setup (baseline)
#   ctr-cuda13.3    b9882, CUDA 13.3  <- same build+toolkit, ONLY the platform differs
#   ctr-cuda12.8    b9879, CUDA 12.8  <- the official image, for the toolkit delta
#
# host vs ctr-cuda13.3   = the WSL2/Docker cost, isolated.
# ctr-cuda13.3 vs 12.8   = what the toolkit alone was worth, which is what made the
#                          first run's +35% pp512 uninterpretable.
$ErrorActionPreference = "Stop"
$scratch = Split-Path -Parent $MyInvocation.MyCommand.Path
$out     = Join-Path $scratch "matched.jsonl"
$HOSTBIN = "C:\selfhosting\llama-cpp\llama-bench.exe"
$IMG13   = "llamabench:b9882-cuda13.3"
$IMG128  = "ghcr.io/ggml-org/llama.cpp:full-cuda-b9879"
$MOUNT   = "C:/selfhosting/models"

$models = @(
  @{ role="fast";  rel="Qwythos-9B-Claude-Mythos-5-1M-MTP-Q4_K_M.gguf"
     args=@("-ngl","99","-fa","on","-ctk","q5_0","-ctv","q4_1","-t","12","-p","512","-n","128","-r","3"); rounds=3 }
  @{ role="embed"; rel="embed/Octen-Embedding-4B.Q8_0.gguf"
     args=@("-ngl","0","-dev","none","-embd","1","-fa","on","-t","12","-b","4096","-ub","4096","-p","512","-n","0","-r","3"); rounds=3 }
  @{ role="main";  rel="Qwopus3.6-35B-A3B-Coder-MTP-Q4_K_M.gguf"
     args=@("-ngl","10","-fa","on","-ctk","q5_0","-ctv","q4_1","-t","12","-p","512","-n","128","-r","2"); rounds=2 }
)

function Emit($rec) { ($rec | ConvertTo-Json -Depth 6 -Compress) | Add-Content -Path $out -Encoding utf8 }
function FreeVram { $v = (nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits) -split ",\s*"; [int]$v[1] - [int]$v[0] }

function Record($raw, $arm, $role, $round, $wall, $vram) {
  try { $rows = $raw | ConvertFrom-Json } catch { Emit @{ ok=$false; arm=$arm; role=$role; round=$round }; return }
  foreach ($r in $rows) {
    Emit @{ ok=$true; arm=$arm; role=$role; round=$round; test="$($r.n_prompt)p/$($r.n_gen)g"
            avg_ts=$r.avg_ts; stddev_ts=$r.stddev_ts; build=$r.build_number
            wall_s=[math]::Round($wall,2); free_vram_mib=$vram }
  }
}

foreach ($m in $models) {
  for ($round = 1; $round -le $m.rounds; $round++) {
    $arms = @(
      @{ name="host-cuda13.3"; run={ & $HOSTBIN -m "C:\selfhosting\models\$($m.rel)" @($m.args) -o json 2>$null } }
      @{ name="ctr-cuda13.3";  run={ docker run --rm --gpus all -v "${MOUNT}:/models:ro" $IMG13 -m "/models/$($m.rel)" @($m.args) -o json 2>$null } }
      @{ name="ctr-cuda12.8";  run={ docker run --rm --gpus all -v "${MOUNT}:/models:ro" --entrypoint /app/llama-bench $IMG128 -m "/models/$($m.rel)" @($m.args) -o json 2>$null } }
    )
    foreach ($a in $arms) {
      $v = FreeVram
      $sw = [Diagnostics.Stopwatch]::StartNew()
      $raw = & $a.run
      $sw.Stop()
      Record $raw $a.name $m.role $round $sw.Elapsed.TotalSeconds $v
      Write-Host "[$($m.role) r$round] $($a.name) $([math]::Round($sw.Elapsed.TotalSeconds,1))s"
    }
  }
}
Write-Host "DONE -> $out"
