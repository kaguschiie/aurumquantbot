@echo off
chcp 65001 >nul
cd /d "%~dp0"
title KriptoQuantBot - guncelleme yayinla
setlocal EnableDelayedExpansion
rem Tek tikla yayin: yeni surum numarasi -> exe derle -> zip + SHA-256 -> GitHub release.
rem Diger bilgisayarlar (abinin PC'si, kanal PC'si) Ayarlar > Guncelleme > "Tek tikla guncelle" ile kurar; veri korunur.
rem Bir kerelik hazirlik: 1) GitHub'da depo acin  2) winget install GitHub.cli  3) gh auth login
rem 4) Bu klasorde guncelleme_ayar.txt icine: REPO=kullanici/depo   5) Uygulamada Ayarlar > Guncelleme deposu ayni depo.

if not exist guncelleme_ayar.txt (
  echo REPO=kullanici/depo> guncelleme_ayar.txt
  echo guncelleme_ayar.txt olusturuldu. Icindeki REPO satirini kendi GitHub deponuzla degistirip tekrar calistirin.
  pause & exit /b 1
)
for /f "usebackq tokens=1,* delims==" %%a in ("guncelleme_ayar.txt") do if /i "%%a"=="REPO" set REPO=%%b
if "%REPO%"=="" goto ayar
if /i "%REPO%"=="kullanici/depo" goto ayar
where gh >nul 2>nul || (echo GitHub CLI bulunamadi. Kurun: winget install GitHub.cli  ^(sonra: gh auth login^) & pause & exit /b 1)
gh auth status >nul 2>nul || (echo GitHub oturumu yok. Bir kez calistirin: gh auth login & pause & exit /b 1)

for /f %%v in ('powershell -NoProfile -Command "Get-Date -Format yyyy.MM.dd.HHmm"') do set VER=%%v
echo Yeni surum: %VER%   Depo: %REPO%
set "NOTLAR="
set /p NOTLAR=Bu surumdeki degisiklikler (kisa, Enter = bos):
if "!NOTLAR!"=="" set "NOTLAR=KriptoQuantBot %VER%"
> surum.txt echo %VER%

echo [1/4] exe derleniyor...
call exe_olustur.bat yayin || goto hata
echo [2/4] paket hazirlaniyor (veri klasoru pakete girmez)...
set ZIP=dist_yayin\KriptoQuantBot_%VER%.zip
if exist "%ZIP%" del /q "%ZIP%"
powershell -NoProfile -Command "$ErrorActionPreference='Stop'; $src='dist_yayin\KriptoQuantBot'; $items=Get-ChildItem $src | Where-Object { $_.Name -ne 'veri' }; Compress-Archive -Path $items.FullName -DestinationPath '%ZIP%' -CompressionLevel Optimal" || goto hata
powershell -NoProfile -Command "(Get-FileHash '%ZIP%' -Algorithm SHA256).Hash.ToLower() + '  KriptoQuantBot_%VER%.zip' | Set-Content -Encoding ascii '%ZIP%.sha256'" || goto hata
echo [3/4] GitHub'a yukleniyor...
gh release create v%VER% "%ZIP%" "%ZIP%.sha256" --repo %REPO% --title "KriptoQuantBot %VER%" --notes "!NOTLAR!" || goto hata
echo [4/4] Tamam: v%VER% yayinlandi. Diger bilgisayarlarda Ayarlar ^> Guncelleme ^> "Tek tikla guncelle".
del /q "%ZIP%" "%ZIP%.sha256" >nul 2>nul
pause
exit /b 0
:ayar
echo guncelleme_ayar.txt icindeki REPO satirini kendi deponuzla doldurun (or. REPO=melik/KriptoQuantBot).
pause & exit /b 1
:hata
echo Yayin basarisiz. Yukaridaki mesaja bakin.
pause
exit /b 1
