/**
 * The 24px bar under the dock: where you are (left), what every session is
 * doing (centre), and what it is costing (right). Glanceable without opening a
 * panel — the whole point of the flow layer.
 *
 * The bar owns the three regions and nothing that goes in them: each item is a
 * `statusContributions` entry on a registered tool (the workspace name is the
 * Files tool's, the active file the Editor's, the chips and counts the Agent's),
 * rendered here in registry order. A capability that wants a line in the status
 * bar declares one — it does not edit this file.
 */

import { statusItems, type StatusRegion } from "../registry";
import { TOOLS } from "../tools";

function Region({ region, className }: { region: StatusRegion; className: string }) {
  return (
    <div className={className}>
      {statusItems(TOOLS, region).map((item) => (
        <item.component key={item.key} />
      ))}
    </div>
  );
}

export function StatusBar() {
  return (
    <div className="wb-statusbar">
      <Region region="left" className="wb-status-left" />
      <Region region="center" className="wb-status-center" />
      <Region region="right" className="wb-status-right" />
    </div>
  );
}
