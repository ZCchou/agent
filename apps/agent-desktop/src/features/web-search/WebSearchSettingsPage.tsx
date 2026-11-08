import { Button } from "../../components/ui/Button";
import { Card } from "../../components/ui/Card";
export function WebSearchSettingsPage({ onBack }: { onBack(): void }) {
  return <Card><h2>联网检索</h2><p>离线模式已停用联网检索。</p><Button onClick={onBack}>返回</Button></Card>;
}
