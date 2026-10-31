export default function ColumnOptions({ columns }) {
  return columns.map((column) => <option key={column.id} value={column.id}>{column.title}</option>);
}
