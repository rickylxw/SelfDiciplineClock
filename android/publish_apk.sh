#!/usr/bin/env bash
# 发布手机端新版本：版本号自增、构建签名 APK、刷新 version.json。
# 之后需要 git commit + push 推到 GitHub，App 端才能通过 jsDelivr 检查到新版。
#
# 用法：android/publish_apk.sh <版本名>    例如：./publish_apk.sh 1.2
set -euo pipefail
cd "$(dirname "$0")"

NEW_NAME="${1:?用法: publish_apk.sh <版本名，如 1.2>}"
GRADLE_BIN="${GRADLE_BIN:-$HOME/.gradle/wrapper/dists/gradle-9.3.1-all/5wooodlkfwsm42ictrailuf0e/gradle-9.3.1/bin/gradle.bat}"
export JAVA_HOME="${JAVA_HOME:-E:\Program Files\Android Studio\jbr}"

OLD_CODE=$(grep -m1 -oE 'versionCode [0-9]+' app/build.gradle | grep -oE '[0-9]+')
NEW_CODE=$((OLD_CODE + 1))
sed -i -e "s/versionCode $OLD_CODE/versionCode $NEW_CODE/" \
       -e "s/versionName \"[^\"]*\"/versionName \"$NEW_NAME\"/" app/build.gradle
echo "版本：v$NEW_NAME（versionCode $OLD_CODE -> $NEW_CODE）"

"$GRADLE_BIN" assembleRelease

mkdir -p apk
cp app/build/outputs/apk/release/app-release.apk apk/timetracker.apk
cat > version.json <<EOF
{
 "versionCode": $NEW_CODE,
 "versionName": "$NEW_NAME",
 "apk": "android/apk/timetracker.apk"
}
EOF

echo "完成：android/version.json + android/apk/timetracker.apk"
echo "下一步：git add -A android && git commit -m \"v$NEW_NAME：...\" && git push"
