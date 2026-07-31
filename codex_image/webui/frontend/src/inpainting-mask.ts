import { getEls } from "./dom";
import { getLegacyBridge, getState } from "./state";
import { translate } from "./i18n";

type MaskTool = "brush" | "eraser" | "focus";
type NormalizedRect = { x: number; y: number; width: number; height: number };
type MaskHistory = { alpha: Uint8ClampedArray; rect: NormalizedRect | null };

interface EditMaskState {
  file: File;
  previewUrl: string;
  baseSource: any;
  width: number;
  height: number;
  source: "upload" | "drawn";
  focused: {
    enabled: boolean;
    rect: NormalizedRect | null;
    context: number;
    feather: number;
    target_size: 1024;
  };
}

type SetUploadedMaskOptions = {
  focused?: Partial<EditMaskState["focused"]> | null;
  silent?: boolean;
};

const editor = {
  open: false,
  session: 0,
  baseSource: null as any,
  baseImage: null as ImageBitmap | null,
  selection: null as HTMLCanvasElement | null,
  tool: "brush" as MaskTool,
  brushSize: 48,
  zoom: 1,
  showMask: true,
  focusedEnabled: false,
  context: 0.35,
  feather: 12,
  drawing: null as null | {
    pointerId: number;
    start: { x: number; y: number };
    last: { x: number; y: number };
  },
  rect: null as NormalizedRect | null,
  history: [] as MaskHistory[],
  historyIndex: -1,
};

let initialized = false;

function legacyMethod(name: string, ...args: any[]): any {
  return getLegacyBridge().methods[name]?.(...args);
}

function maskState(): EditMaskState | null {
  return (getState().editMask as EditMaskState | null) || null;
}

function setMaskState(value: EditMaskState | null): void {
  const state = getState();
  const previous = state.editMask as EditMaskState | null;
  if (previous?.previewUrl && previous.previewUrl !== value?.previewUrl) URL.revokeObjectURL(previous.previewUrl);
  state.editMask = value;
}

function currentBaseSource(): any {
  return getState().images?.[0] || null;
}

function sourceUrl(source: any): string {
  return String(legacyMethod("sourcePreviewUrl", source) || "");
}

function sourceName(source: any): string {
  return String(legacyMethod("sourceName", source) || "input.png");
}

async function fileBitmap(file: Blob): Promise<ImageBitmap> {
  try {
    return await createImageBitmap(file);
  } catch {
    throw new Error(translate("inpainting.maskDecodeFailed"));
  }
}

async function sourceBitmap(source: any): Promise<ImageBitmap> {
  if (!source) throw new Error(translate("inpainting.baseRequired"));
  const url = sourceUrl(source);
  if (!url) throw new Error(translate("inpainting.baseLoadFailed"));
  const response = await fetch(url);
  if (!response.ok) throw new Error(translate("inpainting.baseLoadFailed"));
  return fileBitmap(await response.blob());
}

function canvas2d(canvas: HTMLCanvasElement | null, read = false): CanvasRenderingContext2D {
  const context = canvas?.getContext("2d", read ? { willReadFrequently: true } : undefined);
  if (!context) throw new Error(translate("inpainting.canvasFailed"));
  return context;
}

function fileFromCanvas(canvas: HTMLCanvasElement, name: string): Promise<File> {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (!blob) {
        reject(new Error(translate("inpainting.maskSaveFailed")));
        return;
      }
      resolve(new File([blob], name, { type: "image/png", lastModified: Date.now() }));
    }, "image/png");
  });
}

function selectionHasPixels(canvas: HTMLCanvasElement): boolean {
  const pixels = canvas2d(canvas, true).getImageData(0, 0, canvas.width, canvas.height).data;
  for (let offset = 3; offset < pixels.length; offset += 4) {
    if (pixels[offset]! > 0) return true;
  }
  return false;
}

async function validateUploadedMask(file: File, source = currentBaseSource()): Promise<{ width: number; height: number }> {
  if (!source) throw new Error(translate("inpainting.baseRequired"));
  if (file.type !== "image/png" && !file.name.toLowerCase().endsWith(".png")) {
    throw new Error(translate("inpainting.maskPngRequired"));
  }
  const [base, mask] = await Promise.all([sourceBitmap(source), fileBitmap(file)]);
  try {
    if (base.width !== mask.width || base.height !== mask.height) {
      throw new Error(translate("inpainting.maskSizeMismatch"));
    }
    const check = document.createElement("canvas");
    check.width = mask.width;
    check.height = mask.height;
    const context = canvas2d(check, true);
    context.drawImage(mask, 0, 0);
    const pixels = context.getImageData(0, 0, check.width, check.height).data;
    let transparent = false;
    for (let offset = 3; offset < pixels.length; offset += 4) {
      if (pixels[offset]! < 255) {
        transparent = true;
        break;
      }
    }
    if (!transparent) throw new Error(translate("inpainting.maskTransparencyRequired"));
    return { width: mask.width, height: mask.height };
  } finally {
    base.close();
    mask.close();
  }
}

function defaultFocused(existing?: EditMaskState | null): EditMaskState["focused"] {
  return {
    enabled: Boolean(existing?.focused.enabled),
    rect: existing?.focused.rect || null,
    context: Number(existing?.focused.context ?? 0.35),
    feather: Number(existing?.focused.feather ?? 12),
    target_size: 1024,
  };
}

function restoredFocused(
  value: SetUploadedMaskOptions["focused"],
  existing?: EditMaskState | null,
): EditMaskState["focused"] {
  const fallback = defaultFocused(existing);
  if (value === undefined) return fallback;
  if (!value?.enabled) return { ...fallback, enabled: false, rect: null };
  return {
    enabled: true,
    rect: value.rect || null,
    context: Number(value.context ?? fallback.context),
    feather: Number(value.feather ?? fallback.feather),
    target_size: 1024,
  };
}

async function setUploadedMask(file: File, options: SetUploadedMaskOptions = {}): Promise<void> {
  const baseSource = currentBaseSource();
  const dimensions = await validateUploadedMask(file, baseSource);
  const previous = maskState();
  setMaskState({
    file,
    previewUrl: URL.createObjectURL(file),
    baseSource,
    width: dimensions.width,
    height: dimensions.height,
    source: "upload",
    focused: restoredFocused(options.focused, previous),
  });
  const moreMenu = document.querySelector<HTMLDetailsElement>("#maskMoreMenu");
  if (moreMenu) moreMenu.open = false;
  renderInpaintingControls();
  legacyMethod("updateRequestPreview");
  if (!options.silent) legacyMethod("setStatus", translate("inpainting.maskReady"), "ok");
}

function clearEditMask(options: { silent?: boolean } = {}): void {
  setMaskState(null);
  const els = getEls();
  if (els.maskInput) els.maskInput.value = "";
  const moreMenu = document.querySelector<HTMLDetailsElement>("#maskMoreMenu");
  if (moreMenu) moreMenu.open = false;
  renderInpaintingControls();
  legacyMethod("updateRequestPreview");
  if (!options.silent) legacyMethod("setStatus", translate("inpainting.maskCleared"), "ok");
}

function focusedPayload(): Record<string, any> | null {
  const mask = maskState();
  if (!mask?.focused.enabled) return null;
  return {
    enabled: true,
    rect: mask.focused.rect,
    context: mask.focused.context,
    feather: mask.focused.feather,
    target_size: 1024,
  };
}

function maskForSubmit(): File | null {
  const mask = maskState();
  if (!mask || mask.baseSource !== currentBaseSource()) return null;
  return mask.file;
}

function renderInpaintingControls(): void {
  const els = getEls();
  const state = getState();
  const base = currentBaseSource();
  const visible = state.mode === "edit" && Boolean(base);
  els.maskBlock?.classList.toggle("hidden", !visible);
  if (!visible) return;

  let mask = maskState();
  if (mask && mask.baseSource !== base) {
    clearEditMask({ silent: true });
    mask = null;
  }
  els.maskEmpty?.classList.toggle("hidden", Boolean(mask));
  els.maskPreviewCard?.classList.toggle("hidden", !mask);
  els.maskClearButton?.classList.toggle("hidden", !mask);
  els.maskFocusedControls?.classList.remove("hidden");
  if (els.maskPreview && mask) els.maskPreview.src = mask.previewUrl;
  if (els.maskName) els.maskName.textContent = mask?.file.name || "";
  if (els.maskMeta) {
    const focusedSuffix = mask?.focused.enabled ? ` · ${translate("inpainting.focused")}` : "";
    els.maskMeta.textContent = mask ? `${mask.width}×${mask.height} · ${mask.source === "drawn" ? translate("inpainting.drawn") : translate("inpainting.uploaded")}${focusedSuffix}` : "";
  }
  if (els.maskDrawButton) {
    els.maskDrawButton.textContent = translate(mask ? "inpainting.continue" : "inpainting.start");
  }
  const focusedEnabled = editor.open ? editor.focusedEnabled : Boolean(mask?.focused.enabled);
  const focusedContext = editor.open ? editor.context : Number(mask?.focused.context ?? 0.35);
  const focusedFeather = editor.open ? editor.feather : Number(mask?.focused.feather ?? 12);
  const focusedRect = editor.open ? editor.rect : mask?.focused.rect;
  if (els.maskFocusedEnabled) els.maskFocusedEnabled.checked = focusedEnabled;
  if (els.maskContext) els.maskContext.value = String(focusedContext);
  if (els.maskContextValue) els.maskContextValue.textContent = `${Math.round(Number(els.maskContext?.value || 0.35) * 100)}%`;
  if (els.maskFeather) els.maskFeather.value = String(focusedFeather);
  if (els.maskFeatherValue) els.maskFeatherValue.textContent = `${Number(els.maskFeather?.value || 12)}px`;
  if (els.maskFocusRectStatus) {
    els.maskFocusRectStatus.textContent = focusedRect ? translate("inpainting.focusManual") : translate("inpainting.focusAuto");
  }
}

function editorSelectionCanvas(width: number, height: number): HTMLCanvasElement {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  return canvas;
}

async function selectionFromMask(file: File, width: number, height: number): Promise<HTMLCanvasElement> {
  const bitmap = await fileBitmap(file);
  try {
    const source = document.createElement("canvas");
    source.width = width;
    source.height = height;
    const sourceContext = canvas2d(source, true);
    sourceContext.drawImage(bitmap, 0, 0, width, height);
    const pixels = sourceContext.getImageData(0, 0, width, height);
    for (let offset = 0; offset < pixels.data.length; offset += 4) {
      const selected = 255 - pixels.data[offset + 3]!;
      pixels.data[offset] = 47;
      pixels.data[offset + 1] = 111;
      pixels.data[offset + 2] = 228;
      pixels.data[offset + 3] = selected;
    }
    const selection = editorSelectionCanvas(width, height);
    canvas2d(selection, true).putImageData(pixels, 0, 0);
    return selection;
  } finally {
    bitmap.close();
  }
}

function snapshotEditor(): MaskHistory | null {
  if (!editor.selection) return null;
  const pixels = canvas2d(editor.selection, true).getImageData(0, 0, editor.selection.width, editor.selection.height).data;
  const alpha = new Uint8ClampedArray(editor.selection.width * editor.selection.height);
  for (let sourceOffset = 3, targetOffset = 0; sourceOffset < pixels.length; sourceOffset += 4, targetOffset += 1) {
    alpha[targetOffset] = pixels[sourceOffset]!;
  }
  return {
    alpha,
    rect: editor.rect ? { ...editor.rect } : null,
  };
}

function pushHistory(): void {
  const snapshot = snapshotEditor();
  if (!snapshot) return;
  editor.history = editor.history.slice(0, editor.historyIndex + 1);
  editor.history.push(snapshot);
  const pixelCount = editor.selection ? editor.selection.width * editor.selection.height : 0;
  const historyLimit = pixelCount > 8_000_000 ? 4 : pixelCount > 2_000_000 ? 8 : 20;
  if (editor.history.length > historyLimit) editor.history.shift();
  editor.historyIndex = editor.history.length - 1;
  updateEditorControls();
}

function restoreHistory(index: number): void {
  const snapshot = editor.history[index];
  if (!snapshot || !editor.selection) return;
  const context = canvas2d(editor.selection, true);
  const pixels = context.createImageData(editor.selection.width, editor.selection.height);
  for (let targetOffset = 0, sourceOffset = 0; targetOffset < pixels.data.length; targetOffset += 4, sourceOffset += 1) {
    pixels.data[targetOffset] = 47;
    pixels.data[targetOffset + 1] = 111;
    pixels.data[targetOffset + 2] = 228;
    pixels.data[targetOffset + 3] = snapshot.alpha[sourceOffset]!;
  }
  context.putImageData(pixels, 0, 0);
  editor.rect = snapshot.rect ? { ...snapshot.rect } : null;
  editor.historyIndex = index;
  renderMaskEditor();
  updateEditorControls();
}

function undoMask(): void { if (editor.historyIndex > 0) restoreHistory(editor.historyIndex - 1); }
function redoMask(): void { if (editor.historyIndex < editor.history.length - 1) restoreHistory(editor.historyIndex + 1); }

function editorPoint(event: PointerEvent): { x: number; y: number } {
  const canvas = getEls().maskEditorCanvas as HTMLCanvasElement;
  const rect = canvas.getBoundingClientRect();
  return {
    x: Math.max(0, Math.min(canvas.width, (event.clientX - rect.left) * canvas.width / Math.max(1, rect.width))),
    y: Math.max(0, Math.min(canvas.height, (event.clientY - rect.top) * canvas.height / Math.max(1, rect.height))),
  };
}

function drawSelectionSegment(from: { x: number; y: number }, to: { x: number; y: number }): void {
  if (!editor.selection) return;
  const context = canvas2d(editor.selection);
  context.save();
  context.globalCompositeOperation = editor.tool === "eraser" ? "destination-out" : "source-over";
  context.strokeStyle = "#2f6fe4";
  context.fillStyle = "#2f6fe4";
  context.lineWidth = editor.brushSize;
  context.lineCap = "round";
  context.lineJoin = "round";
  if (Math.hypot(to.x - from.x, to.y - from.y) < 0.5) {
    context.beginPath();
    context.arc(from.x, from.y, editor.brushSize / 2, 0, Math.PI * 2);
    context.fill();
  } else {
    context.beginPath();
    context.moveTo(from.x, from.y);
    context.lineTo(to.x, to.y);
    context.stroke();
  }
  context.restore();
}

function normalizedRect(from: { x: number; y: number }, to: { x: number; y: number }, width: number, height: number): NormalizedRect | null {
  const left = Math.min(from.x, to.x);
  const top = Math.min(from.y, to.y);
  const rectWidth = Math.abs(to.x - from.x);
  const rectHeight = Math.abs(to.y - from.y);
  if (rectWidth < 4 || rectHeight < 4) return null;
  return { x: left / width, y: top / height, width: rectWidth / width, height: rectHeight / height };
}

function handlePointerDown(event: PointerEvent): void {
  if (!editor.open || !editor.selection) return;
  event.preventDefault();
  const point = editorPoint(event);
  if (event.isTrusted) {
    try {
      (event.currentTarget as HTMLElement)?.setPointerCapture?.(event.pointerId);
    } catch {
      // Pointer capture is best-effort; drawing still works without it.
    }
  }
  editor.drawing = { pointerId: event.pointerId, start: point, last: point };
  if (editor.tool !== "focus") drawSelectionSegment(point, point);
  renderMaskEditor();
}

function handlePointerMove(event: PointerEvent): void {
  const drawing = editor.drawing;
  if (!drawing || drawing.pointerId !== event.pointerId || !editor.selection) return;
  event.preventDefault();
  const point = editorPoint(event);
  if (editor.tool === "focus") {
    editor.rect = normalizedRect(drawing.start, point, editor.selection.width, editor.selection.height);
    editor.focusedEnabled = true;
  } else {
    drawSelectionSegment(drawing.last, point);
  }
  drawing.last = point;
  renderMaskEditor();
}

function handlePointerUp(event: PointerEvent): void {
  const drawing = editor.drawing;
  if (!drawing || drawing.pointerId !== event.pointerId || !editor.selection) return;
  event.preventDefault();
  const point = editorPoint(event);
  if (editor.tool === "focus") {
    editor.rect = normalizedRect(drawing.start, point, editor.selection.width, editor.selection.height);
    editor.focusedEnabled = true;
  } else {
    drawSelectionSegment(drawing.last, point);
  }
  editor.drawing = null;
  pushHistory();
  renderMaskEditor();
}

function renderMaskEditor(): void {
  const els = getEls();
  const canvas = els.maskEditorCanvas as HTMLCanvasElement | null;
  const overlay = els.maskEditorOverlay as HTMLCanvasElement | null;
  const stack = els.maskEditorCanvasStack as HTMLElement | null;
  if (!canvas || !overlay || !editor.baseImage || !editor.selection) return;
  if (canvas.width !== editor.baseImage.width || canvas.height !== editor.baseImage.height) {
    canvas.width = editor.baseImage.width;
    canvas.height = editor.baseImage.height;
  }
  if (overlay.width !== canvas.width || overlay.height !== canvas.height) {
    overlay.width = canvas.width;
    overlay.height = canvas.height;
  }
  const context = canvas2d(canvas);
  context.globalCompositeOperation = "source-over";
  context.globalAlpha = 1;
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.drawImage(editor.baseImage, 0, 0);

  const overlayContext = canvas2d(overlay);
  overlayContext.globalCompositeOperation = "source-over";
  overlayContext.globalAlpha = 1;
  overlayContext.clearRect(0, 0, overlay.width, overlay.height);
  if (editor.showMask) {
    overlayContext.globalAlpha = 0.48;
    overlayContext.drawImage(editor.selection, 0, 0);
    overlayContext.globalAlpha = 1;
  }
  if (editor.rect) {
    overlayContext.save();
    overlayContext.strokeStyle = "#ffcc33";
    overlayContext.lineWidth = Math.max(2, 4 / editor.zoom);
    overlayContext.setLineDash([12 / editor.zoom, 8 / editor.zoom]);
    overlayContext.strokeRect(
      editor.rect.x * canvas.width,
      editor.rect.y * canvas.height,
      editor.rect.width * canvas.width,
      editor.rect.height * canvas.height,
    );
    overlayContext.restore();
  }
  const displayWidth = canvas.width * editor.zoom;
  const displayHeight = canvas.height * editor.zoom;
  canvas.style.width = `${displayWidth}px`;
  canvas.style.height = `${displayHeight}px`;
  overlay.style.width = `${displayWidth}px`;
  overlay.style.height = `${displayHeight}px`;
  if (stack) {
    stack.style.width = `${displayWidth}px`;
    stack.style.height = `${displayHeight}px`;
  }
  updateEditorControls();
}

function updateEditorControls(): void {
  const els = getEls();
  document.querySelectorAll<HTMLElement>("[data-mask-tool]").forEach((button) => {
    button.classList.toggle("active", button.dataset.maskTool === editor.tool);
  });
  if (els.maskEditorUndo) els.maskEditorUndo.disabled = editor.historyIndex <= 0;
  if (els.maskEditorRedo) els.maskEditorRedo.disabled = editor.historyIndex >= editor.history.length - 1;
  if (els.maskEditorBrushSize) els.maskEditorBrushSize.value = String(editor.brushSize);
  if (els.maskEditorBrushValue) els.maskEditorBrushValue.textContent = `${editor.brushSize}px`;
  if (els.maskEditorShow) els.maskEditorShow.textContent = editor.showMask ? translate("inpainting.hideMask") : translate("inpainting.showMask");
  if (els.maskEditorZoom) els.maskEditorZoom.textContent = `${Math.round(editor.zoom * 100)}%`;
  if (els.maskEditorFocusStatus) els.maskEditorFocusStatus.textContent = editor.rect ? translate("inpainting.focusManual") : translate("inpainting.focusAuto");
  if (editor.open) renderInpaintingControls();
}

function closeMaskEditor(): void {
  editor.session += 1;
  editor.open = false;
  editor.baseImage?.close();
  editor.baseImage = null;
  editor.selection = null;
  editor.drawing = null;
  editor.history = [];
  editor.historyIndex = -1;
  getEls().maskEditorModal?.classList.add("hidden");
}

async function openMaskEditor(): Promise<void> {
  const baseSource = currentBaseSource();
  if (!baseSource || getState().mode !== "edit") {
    legacyMethod("setStatus", translate("inpainting.baseRequired"), "error");
    return;
  }
  const session = ++editor.session;
  getEls().maskEditorModal?.classList.remove("hidden");
  try {
    const base = await sourceBitmap(baseSource);
    if (session !== editor.session) {
      base.close();
      return;
    }
    const current = maskState();
    const selection = current && current.baseSource === baseSource
      ? await selectionFromMask(current.file, base.width, base.height)
      : editorSelectionCanvas(base.width, base.height);
    if (session !== editor.session) {
      base.close();
      return;
    }
    editor.open = true;
    editor.baseSource = baseSource;
    editor.baseImage = base;
    editor.selection = selection;
    editor.rect = current?.focused.rect || null;
    editor.focusedEnabled = Boolean(current?.focused.enabled);
    editor.context = Number(current?.focused.context ?? 0.35);
    editor.feather = Number(current?.focused.feather ?? 12);
    editor.tool = "brush";
    const canvasWrap = getEls().maskEditorCanvasWrap as HTMLElement | null;
    const availableWidth = Math.max(120, Number(canvasWrap?.clientWidth || 940) - 36);
    const availableHeight = Math.max(120, Number(canvasWrap?.clientHeight || 700) - 36);
    editor.zoom = Math.max(0.1, Math.min(1, availableWidth / base.width, availableHeight / base.height));
    editor.showMask = true;
    editor.history = [];
    editor.historyIndex = -1;
    pushHistory();
    if (getEls().maskEditorTitle) getEls().maskEditorTitle.textContent = `${translate("inpainting.editorTitle")} · ${sourceName(baseSource)}`;
    const advanced = document.querySelector<HTMLDetailsElement>("#maskEditorAdvanced");
    if (advanced) advanced.open = Boolean(current?.focused.enabled);
    renderMaskEditor();
  } catch (error: any) {
    closeMaskEditor();
    legacyMethod("setStatus", error.message || translate("inpainting.baseLoadFailed"), "error");
  }
}

function clearSelection(): void {
  if (!editor.selection) return;
  canvas2d(editor.selection).clearRect(0, 0, editor.selection.width, editor.selection.height);
  pushHistory();
  renderMaskEditor();
}

function invertSelection(): void {
  if (!editor.selection) return;
  const context = canvas2d(editor.selection, true);
  const pixels = context.getImageData(0, 0, editor.selection.width, editor.selection.height);
  for (let offset = 3; offset < pixels.data.length; offset += 4) pixels.data[offset] = 255 - pixels.data[offset]!;
  context.putImageData(pixels, 0, 0);
  pushHistory();
  renderMaskEditor();
}

function autoFocusSelection(): void {
  editor.rect = null;
  editor.focusedEnabled = true;
  pushHistory();
  renderMaskEditor();
}

async function saveMaskEditor(): Promise<void> {
  if (!editor.selection || !editor.baseSource || !selectionHasPixels(editor.selection)) {
    if (getEls().maskEditorStatus) getEls().maskEditorStatus.textContent = translate("inpainting.paintRequired");
    return;
  }
  const output = document.createElement("canvas");
  output.width = editor.selection.width;
  output.height = editor.selection.height;
  const selectionPixels = canvas2d(editor.selection, true).getImageData(0, 0, output.width, output.height);
  const maskPixels = canvas2d(output, true).createImageData(output.width, output.height);
  for (let offset = 0; offset < maskPixels.data.length; offset += 4) {
    maskPixels.data[offset] = 0;
    maskPixels.data[offset + 1] = 0;
    maskPixels.data[offset + 2] = 0;
    maskPixels.data[offset + 3] = 255 - selectionPixels.data[offset + 3]!;
  }
  canvas2d(output, true).putImageData(maskPixels, 0, 0);
  const file = await fileFromCanvas(output, `${sourceName(editor.baseSource).replace(/\.[^.]+$/, "")}-mask.png`);
  const previous = maskState();
  setMaskState({
    file,
    previewUrl: URL.createObjectURL(file),
    baseSource: editor.baseSource,
    width: output.width,
    height: output.height,
    source: "drawn",
    focused: {
      ...defaultFocused(previous),
      enabled: editor.focusedEnabled,
      rect: editor.rect,
      context: editor.context,
      feather: editor.feather,
    },
  });
  closeMaskEditor();
  renderInpaintingControls();
  legacyMethod("updateRequestPreview");
  legacyMethod("setStatus", translate("inpainting.maskReady"), "ok");
}

function updateFocusedFromControls(): void {
  const els = getEls();
  const enabled = Boolean(els.maskFocusedEnabled?.checked);
  const context = Math.max(0, Math.min(1, Number(els.maskContext?.value || 0.35)));
  const feather = Math.max(0, Math.min(128, Number(els.maskFeather?.value || 12)));
  if (editor.open) {
    editor.focusedEnabled = enabled;
    editor.context = context;
    editor.feather = feather;
    renderInpaintingControls();
    return;
  }
  const mask = maskState();
  if (!mask) return;
  mask.focused.enabled = enabled;
  mask.focused.context = context;
  mask.focused.feather = feather;
  renderInpaintingControls();
  legacyMethod("updateRequestPreview");
}

function setTool(tool: MaskTool): void {
  editor.tool = tool;
  updateEditorControls();
}

function changeZoom(delta: number): void {
  editor.zoom = Math.max(0.1, Math.min(4, editor.zoom + delta));
  renderMaskEditor();
}

function bindEvents(): void {
  const els = getEls();
  els.maskInput?.addEventListener("change", async () => {
    const file = els.maskInput.files?.[0];
    if (!file) return;
    try {
      await setUploadedMask(file);
    } catch (error: any) {
      els.maskInput.value = "";
      legacyMethod("setStatus", error.message || translate("inpainting.maskDecodeFailed"), "error");
    }
  });
  els.maskDrawButton?.addEventListener("click", openMaskEditor);
  els.maskReplaceButton?.addEventListener("click", () => els.maskInput?.click());
  els.maskClearButton?.addEventListener("click", () => clearEditMask());
  els.maskFocusedEnabled?.addEventListener("change", updateFocusedFromControls);
  els.maskContext?.addEventListener("input", updateFocusedFromControls);
  els.maskFeather?.addEventListener("input", updateFocusedFromControls);
  els.maskFocusEditButton?.addEventListener("click", openMaskEditor);

  els.maskEditorClose?.addEventListener("click", closeMaskEditor);
  els.maskEditorCancel?.addEventListener("click", closeMaskEditor);
  els.maskEditorSave?.addEventListener("click", saveMaskEditor);
  els.maskEditorModal?.addEventListener("click", (event: MouseEvent) => {
    if (event.target === els.maskEditorModal) closeMaskEditor();
  });
  document.querySelectorAll<HTMLElement>("[data-mask-tool]").forEach((button) => {
    button.addEventListener("click", () => setTool(button.dataset.maskTool as MaskTool));
  });
  els.maskEditorBrushSize?.addEventListener("input", () => {
    editor.brushSize = Number(els.maskEditorBrushSize.value || 48);
    updateEditorControls();
  });
  els.maskEditorUndo?.addEventListener("click", undoMask);
  els.maskEditorRedo?.addEventListener("click", redoMask);
  els.maskEditorClear?.addEventListener("click", clearSelection);
  els.maskEditorInvert?.addEventListener("click", invertSelection);
  els.maskEditorShow?.addEventListener("click", () => { editor.showMask = !editor.showMask; renderMaskEditor(); });
  els.maskEditorAutoFocus?.addEventListener("click", autoFocusSelection);
  els.maskEditorZoomIn?.addEventListener("click", () => changeZoom(0.15));
  els.maskEditorZoomOut?.addEventListener("click", () => changeZoom(-0.15));
  const canvas = els.maskEditorCanvas as HTMLCanvasElement | null;
  canvas?.addEventListener("pointerdown", handlePointerDown);
  canvas?.addEventListener("pointermove", handlePointerMove);
  canvas?.addEventListener("pointerup", handlePointerUp);
  canvas?.addEventListener("pointercancel", handlePointerUp);
}

export function initInpaintingMaskFeature(): void {
  if (initialized) return;
  initialized = true;
  if (getState().editMask === undefined) getState().editMask = null;
  bindEvents();
  Object.assign(getLegacyBridge().methods, {
    clearEditMask,
    currentEditMask: maskForSubmit,
    currentFocusedInpainting: focusedPayload,
    openMaskEditor,
    renderInpaintingControls,
    setUploadedMask,
  });
  renderInpaintingControls();
}
