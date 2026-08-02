# Model LOAD time: host NTFS vs container bind-mount vs container named volume.
#
# This is the measurement that decides the question. handoff-llama-control.md §7
# calls the cold model swap (100-600s) "the single largest performance lever in
# the system"; a container reading GGUFs over Docker Desktop's 9p boundary to the
# Windows filesystem is the one thing that could make it materially worse. The
# named volume (ext4 inside the WSL2 VHDX) is the fix for that, if it works.
#
# Cold is forced with -dio 1 (direct I/O, bypasses the page cache) so the number
# is repeatable instead of a measure of how recently the file was touched.
# Warm is the default mmap path. Both matter: a nightly run swaps repeatedly.
#
# Uses the build+toolkit-matched image so nothing but storage differs.
$ErrorActionPreference = "Stop"
$scratch = Split-Path -Parent $MyInvocation.MyCommand.Path
$out     = Join-Path $scratch "load.jsonl"
$HOSTBIN = "C:\selfhosting\llama-cpp\llama-bench.exe"
$IMG     = "llamabench:b9882-cuda13.3"
$VOL     = "llamabench-models"

$models = @(
  @{ role="embed"; rel="embed/Octen-Embedding-4B.Q8_0.gguf";            gb=3.99;  ngl=@("-ngl","0","-dev","none","-embd","1") }
  @{ role="fast";  rel="Qwythos-9B-Claude-Mythos-5-1M-MTP-Q4_K_M.gguf"; gb=5.48;  ngl=@("-ngl","99") }
  @{ role="main";  rel="Qwopus3.6-35B-A3B-Coder-MTP-Q4_K_M.gguf";       gb=20.22; ngl=@("-ngl","10") }
)
# Trivial workload so total time is load-dominated.
$work = @("-fa","on","-t","12","-p","8","-n","0","-r","1","--no-warmup")

function Emit($rec) { ($rec | ConvertTo-Json -Depth 6 -Compress) | Add-Content -Path $out -Encoding utf8 }
function Time-It($block) {
  $sw = [Diagnostics.Stopwatch]::StartNew()
  $txt = & $block 2>&1 | Out-String
  $sw.Stop()
  return @{ s=$sw.Elapsed.TotalSeconds; ok=($LASTEXITCODE -eq 0); txt=$txt }
}

Write-Host "== fixed overhead (process start + CUDA/backend init, docker run startup) =="
$hostOv = (Time-It { & $HOSTBIN --list-devices }).s
$ctrOv  = (Time-It { docker run --rm --gpus all $IMG --list-devices }).s
Emit @{ kind="overhead"; host_s=[math]::Round($hostOv,2); container_s=[math]::Round($ctrOv,2) }
Write-Host "host $([math]::Round($hostOv,2))s / container $([math]::Round($ctrOv,2))s"

Write-Host "== staging models into named volume =="
docker volume create $VOL | Out-Null
$stage = Time-It {
  docker run --rm -v C:/selfhosting/models:/src:ro -v "${VOL}:/dst" alpine `
    sh -c 'mkdir -p /dst/embed; for f in Qwythos-9B-Claude-Mythos-5-1M-MTP-Q4_K_M.gguf Qwopus3.6-35B-A3B-Coder-MTP-Q4_K_M.gguf; do [ -f "/dst/$f" ] || cp "/src/$f" "/dst/$f"; done; [ -f /dst/embed/Octen-Embedding-4B.Q8_0.gguf ] || cp /src/embed/Octen-Embedding-4B.Q8_0.gguf /dst/embed/; du -sh /dst'
}
Emit @{ kind="stage"; seconds=[math]::Round($stage.s,1); ok=$stage.ok; detail=$stage.txt.Trim() }
Write-Host "staged in $([math]::Round($stage.s,1))s ok=$($stage.ok)"
Write-Host $stage.txt.Trim()

foreach ($m in $models) {
  foreach ($dio in @(1, 0)) {
    $mode = if ($dio -eq 1) { "cold-directio" } else { "warm-mmap" }
    $a = $work + $m.ngl + @("-dio", "$dio")

    $arms = @(
      @{ store="host-ntfs";     ov=$hostOv; run={ & $HOSTBIN -m "C:\selfhosting\models\$($m.rel)" @a -o json } }
      @{ store="ctr-bindmount"; ov=$ctrOv;  run={ docker run --rm --gpus all -v C:/selfhosting/models:/models:ro $IMG -m "/models/$($m.rel)" @a -o json } }
      @{ store="ctr-volume";    ov=$ctrOv;  run={ docker run --rm --gpus all -v "${VOL}:/models:ro" $IMG -m "/models/$($m.rel)" @a -o json } }
    )
    foreach ($arm in $arms) {
      $r = Time-It $arm.run
      $load = [math]::Max($r.s - $arm.ov, 0.01)
      Emit @{ kind="load"; role=$m.role; store=$arm.store; mode=$mode; gb=$m.gb
              wall_s=[math]::Round($r.s,2); load_s=[math]::Round($load,2)
              mbps=[math]::Round($m.gb * 1024 / $load, 0); ok=$r.ok }
      Write-Host ("[{0} {1}] {2}: {3}s  ({4} MB/s) ok={5}" -f `
        $m.role, $mode, $arm.store, [math]::Round($load,1), [math]::Round($m.gb*1024/$load,0), $r.ok)
    }
  }
}
Write-Host "DONE -> $out"
