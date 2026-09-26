# 生成图片解码用的测试夹具（开发机专用；运行期不需要 .NET）。
# 用 System.Drawing 画一张固定图案，分别存成 PNG / GIF / JPEG，
# 再**从存出来的文件里读回像素**当作标准答案（PNG/GIF 精确，JPEG 按 8x8 块求均值），
# 供 tools/test_reported_bugs.py 校验 utils/imgtool.py 的解码是否正确。
#
#   pwsh -File tools/make_image_fixtures.ps1
Add-Type -AssemblyName System.Drawing

$dir = Join-Path $PSScriptRoot 'fixtures'
New-Item -ItemType Directory -Force -Path $dir | Out-Null

$W = 64
$H = 64
$bmp = New-Object System.Drawing.Bitmap($W, $H)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.Clear([System.Drawing.Color]::FromArgb(255, 255, 255))
function Fill($r, $gg, $b, $x, $y, $w, $h) {
    $br = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(255, $r, $gg, $b))
    $g.FillRectangle($br, $x, $y, $w, $h)
    $br.Dispose()
}
Fill 220 40 40 0 0 32 32        # 左上 红
Fill 40 200 60 32 0 32 32       # 右上 绿
Fill 40 70 220 0 32 32 32       # 左下 蓝
Fill 240 210 40 32 32 32 32     # 右下 黄
Fill 255 255 255 24 24 16 16    # 中间 白
$g.Dispose()

$png = Join-Path $dir 'pattern.png'
$gif = Join-Path $dir 'pattern.gif'
$jpg = Join-Path $dir 'pattern.jpg'
$bmp.Save($png, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Save($gif, [System.Drawing.Imaging.ImageFormat]::Gif)
$bmp.Save($jpg, [System.Drawing.Imaging.ImageFormat]::Jpeg)
$bmp.Dispose()

function Open-Bitmap($path) {
    $bytes = [System.IO.File]::ReadAllBytes($path)
    $ms = New-Object System.IO.MemoryStream(, $bytes)
    return New-Object System.Drawing.Bitmap($ms)
}

$samples = @(@(8, 8), @(40, 8), @(8, 40), @(40, 40), @(32, 32), @(4, 4), @(59, 59), @(32, 8))

$result = [ordered]@{}
foreach ($fmt in @('png', 'gif')) {
    $b = Open-Bitmap (Get-Item (Join-Path $dir "pattern.$fmt")).FullName
    $list = @()
    foreach ($s in $samples) {
        $c = $b.GetPixel($s[0], $s[1])
        $list += , @($s[0], $s[1], [int]$c.R, [int]$c.G, [int]$c.B)
    }
    $result[$fmt] = @{ size = @($b.Width, $b.Height); samples = $list }
    $b.Dispose()
}

# JPEG 只解 DC -> 返回 8x8 的块均值，所以按 8x8 块取标准答案
$b = Open-Bitmap $jpg
$blocks = @()
for ($by = 0; $by -lt [int]($b.Height / 8); $by++) {
    for ($bx = 0; $bx -lt [int]($b.Width / 8); $bx++) {
        $rs = 0.0; $gs = 0.0; $bs = 0.0
        for ($y = 0; $y -lt 8; $y++) {
            for ($x = 0; $x -lt 8; $x++) {
                $c = $b.GetPixel($bx * 8 + $x, $by * 8 + $y)
                $rs += $c.R; $gs += $c.G; $bs += $c.B
            }
        }
        $blocks += , @([math]::Round($rs / 64, 1), [math]::Round($gs / 64, 1), [math]::Round($bs / 64, 1))
    }
}
$result['jpg'] = @{ size = @($b.Width, $b.Height); cell = 8
    grid = @([int]($b.Width / 8), [int]($b.Height / 8)); blocks = $blocks }
$b.Dispose()

$json = $result | ConvertTo-Json -Depth 8 -Compress
[System.IO.File]::WriteAllText((Join-Path $dir 'pattern_expected.json'), $json,
    (New-Object System.Text.UTF8Encoding($false)))
Get-ChildItem $dir | ForEach-Object { "$($_.Name) $($_.Length)" }
