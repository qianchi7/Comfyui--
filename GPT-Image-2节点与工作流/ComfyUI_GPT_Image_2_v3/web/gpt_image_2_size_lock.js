// GPT Image 2 — 分辨率/比例 互斥 UI
// =====================================
// 每个宽高比只能选它真正有合法尺寸的分辨率档。
//   1:1  → 1K / 2K            （没有 4K）
//   16:9 / 9:16 / 4:3 / 3:4 / 21:9 → 2K / 4K   （没有 1K）
//   3:1  → 4K                 （只有 4K）
// 切换 aspect_ratio 时，动态过滤 resolution 下拉，并把非法当前值吸附到最近的合法档。
// 这样用户在界面上根本选不出会 502 的非法组合。

import { app } from "../../scripts/app.js";

// 权威尺寸表（与 gpt_image_2_node.py 的 GPT_IMAGE_SIZE_TABLE 保持同步）
const SIZE_TABLE = {
  "1:1":  { "1K": "1024x1024", "2K": "2048x2048" },
  "3:2":  { "1K": "1536x1024", "2K": "2048x1360", "4K": "3520x2352" },
  "2:3":  { "1K": "1024x1536", "2K": "1360x2048", "4K": "2352x3520" },
  "16:9": { "2K": "2048x1152", "4K": "3840x2160" },
  "9:16": { "2K": "1152x2048", "4K": "2160x3840" },
  "4:3":  { "2K": "2048x1536", "4K": "3312x2480" },
  "3:4":  { "2K": "1536x2048", "4K": "2480x3312" },
  "21:9": { "2K": "2688x1152", "4K": "3840x1648" },
  "1:3":  { "2K": "1024x3072", "4K": "1280x3840" },
  "3:1":  { "4K": "3840x1280" },
};
const RES_ORDER = ["1K", "2K", "4K"];

function resolutionsFor(aspectRatio) {
  if (aspectRatio === "auto") return ["auto", ...RES_ORDER];
  const row = SIZE_TABLE[aspectRatio];
  if (!row) return ["auto", ...RES_ORDER];
  return RES_ORDER.filter((r) => row[r]);
}

// 当前分辨率非法时，选一个最近的合法档
function nearestValid(current, valid) {
  if (valid.includes(current)) return current;
  const idx = RES_ORDER.indexOf((current || "").toUpperCase());
  if (idx < 0) return valid[valid.length - 1] || "auto";
  // 先向下找（更小），找不到再向上
  for (let i = idx; i >= 0; i--) if (valid.includes(RES_ORDER[i])) return RES_ORDER[i];
  for (let i = idx; i < RES_ORDER.length; i++) if (valid.includes(RES_ORDER[i])) return RES_ORDER[i];
  return valid[valid.length - 1] || "auto";
}

app.registerExtension({
  name: "GPTImage2.SizeLock",
  beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== "GPTImage2Generator") return;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const r = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;

      const aspectW = this.widgets?.find((w) => w.name === "aspect_ratio");
      const resW = this.widgets?.find((w) => w.name === "resolution");
      if (!aspectW || !resW) return r;

      const applyConstraint = (snap) => {
        const valid = resolutionsFor(aspectW.value);
        // 约束下拉选项
        resW.options = resW.options || {};
        resW.options.values = valid;
        // 吸附非法当前值
        if (snap && !valid.includes(resW.value)) {
          const next = nearestValid(resW.value, valid.filter((v) => v !== "auto"));
          resW.value = valid.includes(next) ? next : valid[valid.length - 1];
          if (resW.callback) resW.callback(resW.value);
        }
        this.setDirtyCanvas?.(true, true);
      };

      // 初次加载：只约束选项，不强行改用户已保存的值（除非真的非法）
      applyConstraint(true);

      // 比例变化 → 重新约束并吸附
      const prevCb = aspectW.callback;
      aspectW.callback = function () {
        const ret = prevCb ? prevCb.apply(this, arguments) : undefined;
        applyConstraint(true);
        return ret;
      };

      return r;
    };
  },
});
