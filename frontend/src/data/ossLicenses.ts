/**
 * 关于页开源许可数据 — 本项目 LICENSE 全文 + 随应用分发的第三方组件清单。
 *
 * 清单口径: 前后端「直接运行时依赖」(frontend package.json dependencies /
 * backend pyproject.toml [project.dependencies]); devDependencies 与测试工具
 * 不随产物分发故不列, 传递依赖由各自包携带许可。
 *
 * 更新方式 (依赖变更后重新生成):
 *   前端: node -e "const fs=require('fs');const d=JSON.parse(fs.readFileSync('package.json','utf8')).dependencies;
 *     for(const n of Object.keys(d)){const p=JSON.parse(fs.readFileSync('node_modules/'+n+'/package.json','utf8'));
 *     console.log(n+'|'+p.version+'|'+p.license)}"
 *   后端: uv run python -c "import tomllib,importlib.metadata as im,json;
 *     deps=tomllib.load(open('pyproject.toml','rb'))['project']['dependencies'];
 *     print(json.dumps([{'name':d.split(';')[0].split('[')[0].split('>')[0].split('<')[0].split('=')[0].split('!')[0].strip(),
 *     'version':im.version(d.split(';')[0].split('[')[0].split('>')[0].split('<')[0].split('=')[0].split('!')[0].strip()),
 *     'license':(im.metadata(d.split(';')[0].split('[')[0].split('>')[0].split('<')[0].split('=')[0].split('!')[0].strip()).get('License') or '')[:40]} for d in deps]))"
 *     (在 backend/ 目录执行)
 */

export const PROJECT_COPYRIGHT = 'Copyright (c) 2026 tick-stock-panel contributors'
export const PROJECT_LICENSE_NAME = 'MIT'
export const PROJECT_REPO_URL = 'https://github.com/shy3130/tick-stock-panel'

export const PROJECT_LICENSE_TEXT = `MIT License

Copyright (c) 2026 tick-stock-panel contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.`

export interface OssLicenseEntry {
  name: string
  version: string
  license: string
}

export const FRONTEND_DEPENDENCIES: OssLicenseEntry[] = [
  { name: '@dnd-kit/core', version: '6.3.1', license: 'MIT' },
  { name: '@dnd-kit/sortable', version: '10.0.0', license: 'MIT' },
  { name: '@dnd-kit/utilities', version: '3.2.2', license: 'MIT' },
  { name: '@fontsource/inter', version: '5.3.0', license: 'OFL-1.1' },
  { name: '@fontsource/jetbrains-mono', version: '5.3.0', license: 'OFL-1.1' },
  { name: '@tanstack/query-sync-storage-persister', version: '5.104.1', license: 'MIT' },
  { name: '@tanstack/react-query', version: '5.104.1', license: 'MIT' },
  { name: '@tanstack/react-query-persist-client', version: '5.104.1', license: 'MIT' },
  { name: '@tanstack/react-virtual', version: '3.14.6', license: 'MIT' },
  { name: 'clsx', version: '2.1.1', license: 'MIT' },
  { name: 'echarts', version: '5.6.0', license: 'Apache-2.0' },
  { name: 'framer-motion', version: '11.18.2', license: 'MIT' },
  { name: 'lightweight-charts', version: '4.2.3', license: 'Apache-2.0' },
  { name: 'lucide-react', version: '0.439.0', license: 'ISC' },
  { name: 'react', version: '18.3.1', license: 'MIT' },
  { name: 'react-dom', version: '18.3.1', license: 'MIT' },
  { name: 'react-grid-layout', version: '2.2.4', license: 'MIT' },
  { name: 'react-router-dom', version: '6.30.3', license: 'MIT' },
  { name: 'tailwind-merge', version: '2.6.1', license: 'MIT' },
]

export const BACKEND_DEPENDENCIES: OssLicenseEntry[] = [
  { name: 'apscheduler', version: '3.11.2', license: 'MIT' },
  { name: 'duckdb', version: '1.5.3', license: 'MIT' },
  { name: 'fastapi', version: '0.136.1', license: 'MIT' },
  { name: 'fastexcel', version: '0.20.2', license: 'MIT' },
  { name: 'httpx', version: '0.28.1', license: 'BSD-3-Clause' },
  { name: 'numba', version: '0.65.1', license: 'BSD' },
  { name: 'openai', version: '2.38.0', license: 'Apache-2.0' },
  { name: 'pandas', version: '3.0.3', license: 'BSD-3-Clause' },
  { name: 'pillow', version: '12.2.0', license: 'MIT-CMU' },
  { name: 'platformdirs', version: '4.10.0', license: 'MIT' },
  { name: 'plyer', version: '2.1.0', license: 'MIT' },
  { name: 'polars', version: '1.44.1', license: 'MIT' },
  { name: 'psutil', version: '7.2.2', license: 'BSD-3-Clause' },
  { name: 'pyarrow', version: '24.0.0', license: 'Apache-2.0' },
  { name: 'pydantic', version: '2.13.4', license: 'MIT' },
  { name: 'pydantic-settings', version: '2.14.1', license: 'MIT' },
  { name: 'pypinyin', version: '0.55.0', license: 'MIT' },
  { name: 'pytesseract', version: '0.3.13', license: 'Apache-2.0' },
  { name: 'python-dotenv', version: '1.2.2', license: 'BSD-3-Clause' },
  { name: 'python-multipart', version: '0.0.30', license: 'Apache-2.0' },
  { name: 'pyyaml', version: '6.0.3', license: 'MIT' },
  { name: 'sse-starlette', version: '3.4.4', license: 'BSD-3-Clause' },
  { name: 'tdxaidata', version: '1.2.2', license: 'MIT' },
  { name: 'tickflow', version: '0.1.25', license: 'MIT' },
  { name: 'uvicorn', version: '0.47.0', license: 'BSD-3-Clause' },
  { name: 'winotify', version: '1.1.0', license: 'MIT' },
]
