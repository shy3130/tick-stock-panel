/** 按当前运行平台给出 Tesseract 安装说明（桌面端未捆绑二进制时使用）。 */
export function getOcrInstallHint(): string {
  const platform =
    (navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData
      ?.platform ||
    navigator.platform ||
    ''
  const p = platform.toLowerCase()

  if (p.includes('win')) {
    return [
      'OCR 引擎不可用：请安装 Tesseract 后重试。',
      '',
      'Windows：',
      '• 下载 UB Mannheim 发行版并安装（勾选将 tesseract 加入 PATH）',
      '• 或执行：choco install tesseract',
      '',
      '安装后重启应用。',
    ].join('\n')
  }

  if (p.includes('mac')) {
    return [
      'OCR 引擎不可用：请安装 Tesseract 后重试。',
      '',
      'macOS：',
      '• 执行：brew install tesseract',
      '',
      '安装后重启应用。',
    ].join('\n')
  }

  return [
    'OCR 引擎不可用：请安装 Tesseract 后重试。',
    '',
    'Linux：安装 tesseract-ocr（及语言包）',
    'Docker：官方镜像已内置，请确认使用对应镜像。',
    '',
    '安装后重启应用。',
  ].join('\n')
}
