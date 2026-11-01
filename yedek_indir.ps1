# Sunucudaki son yedegi bu bilgisayara indirir, 14 kopya saklar. Klasor OneDrive disinda: yedekte API anahtarlari var.
$d = "$env:USERPROFILE\KriptoQuantBot_Yedek"
New-Item -ItemType Directory -Force $d | Out-Null
$f = "$d\yedek_$(Get-Date -Format yyyy-MM-dd).tar.gz"
$k = "$env:USERPROFILE\.ssh\kqb_yedek"
cmd /c "ssh -i `"$k`" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 root@185.246.113.28 > `"$f.part`" 2> `"$d\son_hata.txt`""
if ($LASTEXITCODE -eq 0 -and (Get-Item "$f.part").Length -gt 1000) {
    Move-Item "$f.part" $f -Force
    Remove-Item "$d\son_hata.txt" -ErrorAction SilentlyContinue
    Get-ChildItem $d -Filter "yedek_*.tar.gz" | Sort-Object Name -Descending | Select-Object -Skip 14 | Remove-Item
    "Yedek indirildi: $f"
} else {
    Remove-Item "$f.part" -ErrorAction SilentlyContinue
    "Yedek indirilemedi: $(Get-Content "$d\son_hata.txt" -Raw)"
    exit 1
}
