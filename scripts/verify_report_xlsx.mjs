import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const [inputPath, outputDir] = process.argv.slice(2);
if (!inputPath || !outputDir) {
  throw new Error("Usage: node verify_report_xlsx.mjs <input.xlsx> <output_dir>");
}
await fs.mkdir(outputDir, { recursive: true });
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(inputPath));
const required = ["事件明细", "当前风险", "已解除风险", "商机清单", "来源清单", "数据质量", "抓取运行记录"];
const overview = await workbook.inspect({ kind: "sheet", include: "id,name", maxChars: 4000 });
const overviewText = overview.ndjson;
for (const name of required) {
  if (!overviewText.includes(name)) throw new Error(`Missing required sheet: ${name}`);
  const check = await workbook.inspect({
    kind: "table",
    sheetId: name,
    range: "A1:L20",
    include: "values,formulas",
    tableMaxRows: 20,
    tableMaxCols: 12,
    maxChars: 5000,
  });
  if (!check.ndjson.includes("values")) throw new Error(`Unable to inspect sheet: ${name}`);
  const preview = await workbook.render({ sheetName: name, autoCrop: "all", scale: 1.2, format: "png" });
  await fs.writeFile(`${outputDir}/${required.indexOf(name) + 1}-${name}.png`, new Uint8Array(await preview.arrayBuffer()));
}
const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 100 },
  summary: "final formula error scan",
  maxChars: 3000,
});
if (/\"match\"/.test(errors.ndjson)) throw new Error(`Formula error found: ${errors.ndjson}`);
console.log(JSON.stringify({ sheets: required, previews: required.length, formulaErrors: 0 }, null, 2));
