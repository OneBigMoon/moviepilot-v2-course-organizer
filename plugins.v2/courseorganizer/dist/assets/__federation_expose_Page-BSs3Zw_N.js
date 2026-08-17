import { importShared } from './__federation_fn_import-JrT3xvdd.js';
import { _ as _export_sfc } from './_plugin-vue_export-helper-pcqpp-6-.js';

const {createElementVNode:_createElementVNode,createTextVNode:_createTextVNode,resolveComponent:_resolveComponent,withCtx:_withCtx,createVNode:_createVNode,toDisplayString:_toDisplayString,openBlock:_openBlock,createBlock:_createBlock,createCommentVNode:_createCommentVNode,renderList:_renderList,Fragment:_Fragment,createElementBlock:_createElementBlock} = await importShared('vue');


const _hoisted_1 = {
  class: "course-review-page",
  "aria-labelledby": "course-review-title"
};
const _hoisted_2 = { class: "course-review-toolbar" };
const _hoisted_3 = { class: "d-flex flex-wrap ga-2" };
const _hoisted_4 = { class: "course-review-name" };
const _hoisted_5 = { class: "course-review-edit-cell" };
const _hoisted_6 = { class: "d-flex align-center ga-2 mb-2" };
const _hoisted_7 = {
  key: 1,
  role: "status",
  "aria-live": "polite",
  class: "text-caption text-medium-emphasis mt-1"
};
const _hoisted_8 = { key: 0 };
const _hoisted_9 = { class: "course-review-library-cell" };
const _hoisted_10 = ["title"];
const _hoisted_11 = { class: "course-review-actions text-right" };
const _hoisted_12 = {
  key: 8,
  class: "course-review-cards"
};
const _hoisted_13 = { class: "d-flex align-center ga-2 mb-2" };
const _hoisted_14 = {
  key: 1,
  role: "status",
  "aria-live": "polite",
  class: "text-caption text-medium-emphasis mb-2"
};
const _hoisted_15 = { key: 0 };
const _hoisted_16 = ["title"];
const _hoisted_17 = { class: "d-flex flex-wrap align-center ga-2" };

const {computed,onMounted,onUnmounted,ref} = await importShared('vue');



const _sfc_main = {
  __name: 'Page',
  props: {
  api: { type: Object, default: () => ({}) },
},
  emits: ['close'],
  setup(__props, { expose: __expose, emit: __emit }) {

const props = __props;
const emit = __emit;

const loading = ref(false);
const error = ref('');
const notice = ref('');
const items = ref([]);
const libraries = ref([]);
const directoryRules = ref([]);
const rulesMessage = ref('');
const monitoringEnabled = ref(false);
const settingsUrl = ref('#/setting');
const savingKeys = ref([]);
const organizingKey = ref('');
const tmdbLoadingKeys = ref([]);
const tmdbCandidates = ref({});
let fileTransferSource = null;
const fileTransferText = ref('');
const fileTransferValue = ref(null);
const fileTransferSeenActive = ref(false);

const hasItems = computed(() => items.value.length > 0);

function unwrap(response) {
  const body = response && Object.prototype.hasOwnProperty.call(response, 'success')
    ? response
    : (response?.data ?? response);
  if (body?.success === false) {
    throw new Error(body.message || '请求失败')
  }
  return body?.data ?? body ?? {}
}

function errorMessage(errorValue, fallback) {
  return errorValue?.message || fallback
}

function hasKey(refValue, key) {
  return refValue.value.includes(key)
}

function addKey(refValue, key) {
  if (!refValue.value.includes(key)) {
    refValue.value = [...refValue.value, key];
  }
}

function removeKey(refValue, key) {
  refValue.value = refValue.value.filter(item => item !== key);
}

function sanitizeProgressText(value) {
  return (typeof value === 'string' ? value : '').trim()
}

function stopFileTransferProgress() {
  if (!fileTransferSource) {
    return
  }
  fileTransferSource.close();
  fileTransferSource = null;
}

function startFileTransferProgress() {
  stopFileTransferProgress();
  fileTransferText.value = '正在校验目录并生成目标名称…';
  fileTransferValue.value = null;
  fileTransferSeenActive.value = false;
  try {
    const source = new EventSource('/api/v1/system/progress/filetransfer', {
      withCredentials: true,
    });
    source.onmessage = event => {
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch {
        return
      }
      if (!payload || typeof payload !== 'object' || typeof payload.enable !== 'boolean') {
        return
      }
      if (payload.enable) {
        fileTransferSeenActive.value = true;
        if (typeof payload.text === 'string' && payload.text.trim()) {
          fileTransferText.value = sanitizeProgressText(payload.text);
        }
        if (Number.isFinite(payload.value)) {
          const clipped = Math.max(0, Math.min(100, Number(payload.value)));
          fileTransferValue.value = Math.round(clipped);
        } else {
          fileTransferValue.value = null;
        }
        return
      }
      if (fileTransferSeenActive.value) {
        fileTransferText.value = '文件移动完成，正在写入整理记录…';
        fileTransferValue.value = 100;
      }
    };
    source.onerror = () => {
      // keep fallback indeterminate if EventSource is unavailable
    };
    fileTransferSource = source;
  } catch {
    // fallback to local text/value only
  }
}

async function loadReview() {
  loading.value = true;
  error.value = '';
  try {
    const response = await props.api.get('plugin/CourseOrganizer/review');
    const data = unwrap(response);
    items.value = Array.isArray(data) ? data : (data.items || []);
    if (Array.isArray(data?.libraries) && data.libraries.length) {
      libraries.value = data.libraries;
    } else {
      libraries.value = [];
    }
    directoryRules.value = Array.isArray(data?.directory_rules) ? data.directory_rules : [];
    rulesMessage.value = data?.rules_message || '';
    monitoringEnabled.value = Boolean(data?.monitoring_enabled);
    settingsUrl.value = data?.settings_url || '#/setting';
    tmdbCandidates.value = {};
  } catch (loadError) {
    error.value = errorMessage(loadError, '加载人工复核列表失败');
  } finally {
    loading.value = false;
  }
}

async function refreshReview() {
  loading.value = true;
  error.value = '';
  try {
    await props.api.post('plugin/CourseOrganizer/review/refresh');
    await loadReview();
    notice.value = '预览已重新扫描';
  } catch (refreshError) {
    error.value = errorMessage(refreshError, '刷新预览失败，请检查源目录后重试');
  } finally {
    loading.value = false;
  }
}

async function searchTmdb(row) {
  if (isSaving(row) || isTmdbLoading(row)) return
  addKey(tmdbLoadingKeys, row.raw_title);
  error.value = '';
  notice.value = '';
  tmdbCandidates.value = { ...tmdbCandidates.value, [row.raw_title]: [] };
  try {
    const response = await props.api.post('plugin/CourseOrganizer/review/tmdb/search', {
      raw_title: row.raw_title,
      revision: row.revision,
    });
    const data = unwrap(response);
    const candidates = Array.isArray(data?.items) ? data.items : [];
    tmdbCandidates.value = { ...tmdbCandidates.value, [row.raw_title]: candidates };
    notice.value = data?.message || '已找到 TMDB 候选';
  } catch (searchError) {
    error.value = errorMessage(searchError, '搜索 TMDB 候选失败，请刷新后重试');
  } finally {
    removeKey(tmdbLoadingKeys, row.raw_title);
  }
}

async function associateTmdb(row, candidate) {
  if (isSaving(row) || isTmdbLoading(row) || !candidate?.candidate_key) return
  addKey(savingKeys, row.raw_title);
  error.value = '';
  notice.value = '';
  try {
    const response = await props.api.post('plugin/CourseOrganizer/review/tmdb/associate', {
      raw_title: row.raw_title,
      revision: row.revision,
      candidate_key: candidate.candidate_key,
    });
    const data = unwrap(response);
    tmdbCandidates.value = { ...tmdbCandidates.value, [row.raw_title]: [] };
    notice.value = data?.final_title
      ? `已关联 TMDB：${data.final_title}`
      : (data?.message || '已保存 TMDB 关联');
    const updated = getUpdatedRow(row.raw_title, data);
    if (updated) {
      items.value = replaceRow(row.raw_title, updated);
    }
  } catch (associateError) {
    error.value = errorMessage(associateError, '保存 TMDB 关联失败，请刷新后重试');
  } finally {
    removeKey(savingKeys, row.raw_title);
  }
}

async function saveReview(row, action) {
  if (isSaving(row) || isTmdbLoading(row)) return
  if (action === 'confirm' && organizingKey.value) return
  if (action === 'confirm' && row.association_required) {
    error.value = '请先按名称搜索并关联 TMDB，再确认整理';
    return
  }
  if (action === 'confirm' && (!row.final_title || !row.target_library)) {
    error.value = '请填写建议名称并选择目标媒体库';
    return
  }
  error.value = '';
  notice.value = '';
  const payload = {
    raw_title: row.raw_title,
    revision: row.revision,
    action,
  };
  if (action === 'confirm') {
    payload.final_title = row.final_title;
    payload.target_library = row.target_library;
    startFileTransferProgress();
    organizingKey.value = row.raw_title;
  } else {
    addKey(savingKeys, row.raw_title);
  }
  try {
    const response = await props.api.post('plugin/CourseOrganizer/review', payload);
    const data = unwrap(response);
    if (action === 'confirm') {
      items.value = items.value.filter(item => item.raw_title !== row.raw_title);
      const nextCandidates = { ...tmdbCandidates.value };
      delete nextCandidates[row.raw_title];
      tmdbCandidates.value = nextCandidates;
      notice.value = '整理完成';
    } else {
      notice.value = data?.message || '已保存人工决定';
      const updated = getUpdatedRow(row.raw_title, data);
      if (updated) {
        items.value = replaceRow(row.raw_title, updated);
      }
    }
  } catch (saveError) {
    error.value = errorMessage(
      saveError,
      action === 'confirm' ? '单条整理失败，记录已保留，请重试' : '保存人工决定失败，请刷新后重试',
    );
  } finally {
    if (action === 'confirm') {
      stopFileTransferProgress();
      organizingKey.value = '';
    } else {
      removeKey(savingKeys, row.raw_title);
    }
  }
}

function getUpdatedRow(rawTitle, payload) {
  if (payload && typeof payload === 'object') {
    if (payload.raw_title) {
      return payload
    }
    if (payload.row && payload.row.raw_title) {
      return payload.row
    }
    if (payload.item && payload.item.raw_title) {
      return payload.item
    }
  }
  if (rawTitle && rawTitle === payload?.raw_title) {
    return payload
  }
  return null
}

function replaceRow(rawTitle, nextRow) {
  return items.value.map(item => (item.raw_title === rawTitle ? nextRow : item))
}

function isSaving(row) {
  return hasKey(savingKeys, row.raw_title)
}

function isTmdbLoading(row) {
  return hasKey(tmdbLoadingKeys, row.raw_title)
}

function isOrganizing(row) {
  return organizingKey.value === row.raw_title
}

function organizingProgress() {
  return ` ${fileTransferValue.value === null ? '' : `（${fileTransferValue.value}%）`}`
}

function hasOrganizingValue() {
  return fileTransferValue.value !== null && fileTransferSeenActive.value
}

function organizingStatusText() {
  if (!fileTransferText.value) {
    return '整理中'
  }
  return `${fileTransferText.value}${organizingProgress()}`
}

function libraryLabel(row) {
  return libraries.value.find(item => item.value === row.target_library)?.title || '待确认'
}

function hasLibrary(row) {
  return libraries.value.some(item => item.value === row.target_library)
}

function canConfirm(row) {
  return !row.source_pending && hasLibrary(row) && !row.association_required
}

function isSourcePending(row) {
  return Boolean(row.source_pending)
}

function statusChipColor(row) {
  if (isSourcePending(row)) return 'info'
  if (row.status_label === '可以整理') return 'success'
  if (row.status_label === '已跳过') return 'default'
  return 'warning'
}

function targetPath(row) {
  const library = libraries.value.find(item => item.value === row.target_library);
  if (library?.path && row.final_title) {
    return `${String(library.path).replace(/\/$/, '')}/${row.final_title}`
  }
  return row.target_path || row.target_output_root || '待确认'
}

function tmdbCandidatesFor(row) {
  return tmdbCandidates.value[row.raw_title] || []
}

onMounted(loadReview);
onUnmounted(stopFileTransferProgress);

__expose({ loadReview, items, loading, savingKeys, tmdbCandidates });

return (_ctx, _cache) => {
  const _component_VBtn = _resolveComponent("VBtn");
  const _component_VAlert = _resolveComponent("VAlert");
  const _component_VChip = _resolveComponent("VChip");
  const _component_VSheet = _resolveComponent("VSheet");
  const _component_VProgressLinear = _resolveComponent("VProgressLinear");
  const _component_VTextField = _resolveComponent("VTextField");
  const _component_VSelect = _resolveComponent("VSelect");
  const _component_VTable = _resolveComponent("VTable");
  const _component_VCardTitle = _resolveComponent("VCardTitle");
  const _component_VSpacer = _resolveComponent("VSpacer");
  const _component_VCardText = _resolveComponent("VCardText");
  const _component_VCard = _resolveComponent("VCard");

  return (_openBlock(), _createElementBlock("section", _hoisted_1, [
    _createElementVNode("header", _hoisted_2, [
      _cache[2] || (_cache[2] = _createElementVNode("div", null, [
        _createElementVNode("h1", {
          id: "course-review-title",
          class: "text-h5"
        }, "安全预览与人工确认"),
        _createElementVNode("p", { class: "text-body-2 text-medium-emphasis mb-0" }, " 修改名称和目标媒体库后，确认会立即整理这一条；失败时保留记录，可重试，不会触发全量扫描。 ")
      ], -1)),
      _createVNode(_component_VBtn, {
        "prepend-icon": "mdi-refresh",
        variant: "tonal",
        loading: loading.value,
        "aria-label": "刷新人工复核列表",
        onClick: refreshReview
      }, {
        default: _withCtx(() => [...(_cache[1] || (_cache[1] = [
          _createTextVNode(" 刷新 ", -1)
        ]))]),
        _: 1
      }, 8, ["loading"]),
      _createVNode(_component_VBtn, {
        icon: "mdi-close",
        variant: "text",
        "aria-label": "关闭人工复核",
        onClick: _cache[0] || (_cache[0] = $event => (emit('close')))
      })
    ]),
    _createVNode(_component_VAlert, {
      type: "info",
      variant: "tonal",
      class: "mb-4",
      role: "note"
    }, {
      append: _withCtx(() => [
        _createVNode(_component_VBtn, {
          href: settingsUrl.value,
          variant: "tonal",
          color: "primary",
          "prepend-icon": "mdi-folder-cog"
        }, {
          default: _withCtx(() => [...(_cache[3] || (_cache[3] = [
            _createTextVNode(" 打开目录设置 ", -1)
          ]))]),
          _: 1
        }, 8, ["href"])
      ]),
      default: _withCtx(() => [
        _cache[4] || (_cache[4] = _createTextVNode(" 目录、搬运方式、重命名、刮削、通知和整理历史均来自 MoviePilot「设置 → 存储 & 目录」。 ", -1))
      ]),
      _: 1
    }),
    (rulesMessage.value)
      ? (_openBlock(), _createBlock(_component_VAlert, {
          key: 0,
          type: "warning",
          variant: "tonal",
          class: "mb-4",
          role: "alert"
        }, {
          default: _withCtx(() => [
            _createTextVNode(_toDisplayString(rulesMessage.value), 1)
          ]),
          _: 1
        }))
      : _createCommentVNode("", true),
    (monitoringEnabled.value)
      ? (_openBlock(), _createBlock(_component_VAlert, {
          key: 1,
          type: "warning",
          variant: "tonal",
          class: "mb-4",
          role: "alert"
        }, {
          default: _withCtx(() => [...(_cache[5] || (_cache[5] = [
            _createTextVNode(" 当前匹配规则仍启用了 MoviePilot 自动监控。人工复核期间请在目录设置中关闭这些规则的监控， 避免文件在确认前被自动整理。 ", -1)
          ]))]),
          _: 1
        }))
      : _createCommentVNode("", true),
    (directoryRules.value.length)
      ? (_openBlock(), _createBlock(_component_VSheet, {
          key: 2,
          border: "",
          rounded: "",
          class: "course-directory-rules mb-4 pa-3"
        }, {
          default: _withCtx(() => [
            _cache[6] || (_cache[6] = _createElementVNode("div", { class: "text-subtitle-2 mb-2" }, "MoviePilot 当前目录规则", -1)),
            _createElementVNode("div", _hoisted_3, [
              (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(directoryRules.value, (rule) => {
                return (_openBlock(), _createBlock(_component_VChip, {
                  key: `${rule.value}:${rule.download_path}:${rule.path}`,
                  variant: "tonal",
                  color: "primary",
                  title: `${rule.download_path} → ${rule.path}`
                }, {
                  default: _withCtx(() => [
                    _createTextVNode(_toDisplayString(rule.title) + "：" + _toDisplayString(rule.download_path) + " → " + _toDisplayString(rule.path), 1)
                  ]),
                  _: 2
                }, 1032, ["title"]))
              }), 128))
            ])
          ]),
          _: 1
        }))
      : _createCommentVNode("", true),
    (error.value)
      ? (_openBlock(), _createBlock(_component_VAlert, {
          key: 3,
          type: "error",
          variant: "tonal",
          class: "mb-4",
          role: "alert"
        }, {
          default: _withCtx(() => [
            _createTextVNode(_toDisplayString(error.value), 1)
          ]),
          _: 1
        }))
      : _createCommentVNode("", true),
    (notice.value)
      ? (_openBlock(), _createBlock(_component_VAlert, {
          key: 4,
          type: "success",
          variant: "tonal",
          class: "mb-4",
          role: "status"
        }, {
          default: _withCtx(() => [
            _createTextVNode(_toDisplayString(notice.value), 1)
          ]),
          _: 1
        }))
      : _createCommentVNode("", true),
    (loading.value)
      ? (_openBlock(), _createBlock(_component_VProgressLinear, {
          key: 5,
          indeterminate: "",
          color: "primary",
          "aria-label": "正在加载"
        }))
      : (!hasItems.value)
        ? (_openBlock(), _createBlock(_component_VAlert, {
            key: 6,
            type: "info",
            variant: "tonal",
            role: "status"
          }, {
            default: _withCtx(() => [...(_cache[7] || (_cache[7] = [
              _createTextVNode(" 暂无可复核记录。运行安全预览后，这里会显示待确认目录。 ", -1)
            ]))]),
            _: 1
          }))
        : (_openBlock(), _createBlock(_component_VSheet, {
            key: 7,
            border: "",
            rounded: "",
            class: "course-review-table-shell"
          }, {
            default: _withCtx(() => [
              _createVNode(_component_VTable, {
                class: "course-review-table",
                density: "comfortable"
              }, {
                default: _withCtx(() => [
                  _cache[18] || (_cache[18] = _createElementVNode("thead", null, [
                    _createElementVNode("tr", null, [
                      _createElementVNode("th", { scope: "col" }, "原始名称"),
                      _createElementVNode("th", { scope: "col" }, "建议名称（可改）"),
                      _createElementVNode("th", { scope: "col" }, "目标媒体库"),
                      _createElementVNode("th", { scope: "col" }, "目标位置（只读）"),
                      _createElementVNode("th", { scope: "col" }, "状态"),
                      _createElementVNode("th", {
                        scope: "col",
                        class: "text-right"
                      }, "操作")
                    ])
                  ], -1)),
                  _createElementVNode("tbody", null, [
                    (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(items.value, (row) => {
                      return (_openBlock(), _createElementBlock("tr", {
                        key: row.raw_title
                      }, [
                        _createElementVNode("td", _hoisted_4, _toDisplayString(row.raw_title), 1),
                        _createElementVNode("td", _hoisted_5, [
                          _createElementVNode("div", _hoisted_6, [
                            _createVNode(_component_VChip, {
                              size: "small",
                              variant: "tonal",
                              color: "primary"
                            }, {
                              default: _withCtx(() => [
                                _createTextVNode(_toDisplayString(row.recognition_source_label || '本地'), 1)
                              ]),
                              _: 2
                            }, 1024),
                            _cache[8] || (_cache[8] = _createElementVNode("span", { class: "text-caption text-medium-emphasis" }, "识别来源", -1))
                          ]),
                          _createVNode(_component_VTextField, {
                            modelValue: row.final_title,
                            "onUpdate:modelValue": $event => ((row.final_title) = $event),
                            "aria-label": `建议名称：${row.raw_title}`,
                            "hide-details": "",
                            density: "comfortable",
                            variant: "outlined",
                            autocomplete: "off",
                            disabled: isSaving(row) || isOrganizing(row)
                          }, null, 8, ["modelValue", "onUpdate:modelValue", "aria-label", "disabled"]),
                          (isOrganizing(row))
                            ? (_openBlock(), _createBlock(_component_VProgressLinear, {
                                key: 0,
                                indeterminate: !hasOrganizingValue(),
                                "model-value": fileTransferValue.value || 0,
                                color: "primary",
                                class: "mt-2",
                                "aria-label": "正在整理"
                              }, null, 8, ["indeterminate", "model-value"]))
                            : _createCommentVNode("", true),
                          (isOrganizing(row))
                            ? (_openBlock(), _createElementBlock("div", _hoisted_7, _toDisplayString(organizingStatusText()), 1))
                            : _createCommentVNode("", true),
                          _createVNode(_component_VBtn, {
                            class: "mt-2",
                            variant: "tonal",
                            "min-width": "132",
                            loading: isTmdbLoading(row),
                            disabled: isSourcePending(row) || isSaving(row) || isTmdbLoading(row) || isOrganizing(row),
                            "aria-label": `按名称搜索 TMDB：${row.raw_title}`,
                            onClick: $event => (searchTmdb(row))
                          }, {
                            default: _withCtx(() => [...(_cache[9] || (_cache[9] = [
                              _createTextVNode(" 按名称搜索 TMDB ", -1)
                            ]))]),
                            _: 1
                          }, 8, ["loading", "disabled", "aria-label", "onClick"]),
                          _cache[13] || (_cache[13] = _createElementVNode("div", { class: "text-caption text-medium-emphasis mt-1" }, "按当前目录名称搜索，无需输入 TMDB ID", -1)),
                          (isSourcePending(row))
                            ? (_openBlock(), _createBlock(_component_VAlert, {
                                key: 2,
                                type: "info",
                                density: "compact",
                                variant: "tonal",
                                class: "mt-2",
                                role: "status"
                              }, {
                                default: _withCtx(() => [...(_cache[10] || (_cache[10] = [
                                  _createTextVNode(" 源目录暂不稳定，无法执行搜索、关联或整理，请稍后点击刷新重试 ", -1)
                                ]))]),
                                _: 1
                              }))
                            : (row.association_required)
                              ? (_openBlock(), _createBlock(_component_VAlert, {
                                  key: 3,
                                  type: "warning",
                                  density: "compact",
                                  variant: "tonal",
                                  class: "mt-2"
                                }, {
                                  default: _withCtx(() => [...(_cache[11] || (_cache[11] = [
                                    _createTextVNode(" 整理前需先关联可靠媒体信息 ", -1)
                                  ]))]),
                                  _: 1
                                }))
                              : _createCommentVNode("", true),
                          (tmdbCandidatesFor(row).length)
                            ? (_openBlock(), _createBlock(_component_VSheet, {
                                key: 4,
                                border: "",
                                rounded: "",
                                class: "course-tmdb-candidates mt-2 pa-2"
                              }, {
                                default: _withCtx(() => [
                                  _cache[12] || (_cache[12] = _createElementVNode("div", { class: "text-caption text-medium-emphasis mb-1" }, "请选择匹配的 TMDB 作品", -1)),
                                  (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(tmdbCandidatesFor(row), (candidate) => {
                                    return (_openBlock(), _createBlock(_component_VBtn, {
                                      key: candidate.candidate_key,
                                      block: "",
                                      class: "course-tmdb-candidate mb-1",
                                      variant: "text",
                                      disabled: isSaving(row) || isTmdbLoading(row) || isOrganizing(row),
                                      onClick: $event => (associateTmdb(row, candidate))
                                    }, {
                                      default: _withCtx(() => [
                                        _createTextVNode(_toDisplayString(candidate.title), 1),
                                        (candidate.year)
                                          ? (_openBlock(), _createElementBlock("span", _hoisted_8, "（" + _toDisplayString(candidate.year) + "）", 1))
                                          : _createCommentVNode("", true),
                                        _createTextVNode(" · " + _toDisplayString(candidate.label || candidate.media_type), 1)
                                      ]),
                                      _: 2
                                    }, 1032, ["disabled", "onClick"]))
                                  }), 128))
                                ]),
                                _: 2
                              }, 1024))
                            : _createCommentVNode("", true)
                        ]),
                        _createElementVNode("td", _hoisted_9, [
                          _createVNode(_component_VSelect, {
                            modelValue: row.target_library,
                            "onUpdate:modelValue": $event => ((row.target_library) = $event),
                            items: libraries.value,
                            "item-title": "title",
                            "item-value": "value",
                            "aria-label": `目标媒体库：${row.raw_title}`,
                            "hide-details": "",
                            density: "comfortable",
                            variant: "outlined",
                            disabled: isSaving(row) || isOrganizing(row)
                          }, null, 8, ["modelValue", "onUpdate:modelValue", "items", "aria-label", "disabled"])
                        ]),
                        _createElementVNode("td", {
                          class: "course-review-path",
                          title: targetPath(row)
                        }, _toDisplayString(targetPath(row)), 9, _hoisted_10),
                        _createElementVNode("td", null, [
                          (isOrganizing(row))
                            ? (_openBlock(), _createBlock(_component_VChip, {
                                key: 0,
                                size: "small",
                                variant: "tonal",
                                color: "info",
                                "aria-label": "整理中",
                                class: "course-review-organizing-chip"
                              }, {
                                default: _withCtx(() => [...(_cache[14] || (_cache[14] = [
                                  _createTextVNode(" 整理中 ", -1)
                                ]))]),
                                _: 1
                              }))
                            : (_openBlock(), _createBlock(_component_VChip, {
                                key: 1,
                                size: "small",
                                variant: "tonal",
                                color: statusChipColor(row),
                                "aria-label": `状态：${row.status_label || '需要确认'}`
                              }, {
                                default: _withCtx(() => [
                                  _createTextVNode(_toDisplayString(row.status_label || '需要确认'), 1)
                                ]),
                                _: 2
                              }, 1032, ["color", "aria-label"]))
                        ]),
                        _createElementVNode("td", _hoisted_11, [
                          _createVNode(_component_VBtn, {
                            color: "primary",
                            variant: "tonal",
                            "min-width": "108",
                            loading: isOrganizing(row),
                            disabled: Boolean(organizingKey.value) || !canConfirm(row) || isTmdbLoading(row),
                            "aria-label": `确认整理：${row.raw_title}`,
                            onClick: $event => (saveReview(row, 'confirm'))
                          }, {
                            default: _withCtx(() => [...(_cache[15] || (_cache[15] = [
                              _createTextVNode(" 保存并整理 ", -1)
                            ]))]),
                            _: 1
                          }, 8, ["loading", "disabled", "aria-label", "onClick"]),
                          (row.status_label !== '已跳过')
                            ? (_openBlock(), _createBlock(_component_VBtn, {
                                key: 0,
                                variant: "text",
                                "min-width": "76",
                                disabled: isSourcePending(row) || isSaving(row) || isOrganizing(row),
                                "aria-label": `跳过：${row.raw_title}`,
                                onClick: $event => (saveReview(row, 'ignore'))
                              }, {
                                default: _withCtx(() => [...(_cache[16] || (_cache[16] = [
                                  _createTextVNode(" 跳过 ", -1)
                                ]))]),
                                _: 1
                              }, 8, ["disabled", "aria-label", "onClick"]))
                            : (_openBlock(), _createBlock(_component_VBtn, {
                                key: 1,
                                variant: "text",
                                "min-width": "76",
                                disabled: isSourcePending(row) || Boolean(organizingKey.value) || !canConfirm(row) || isTmdbLoading(row),
                                "aria-label": `重新确认：${row.raw_title}`,
                                onClick: $event => (saveReview(row, 'confirm'))
                              }, {
                                default: _withCtx(() => [...(_cache[17] || (_cache[17] = [
                                  _createTextVNode(" 重新确认 ", -1)
                                ]))]),
                                _: 1
                              }, 8, ["disabled", "aria-label", "onClick"]))
                        ])
                      ]))
                    }), 128))
                  ])
                ]),
                _: 1
              })
            ]),
            _: 1
          })),
    (hasItems.value)
      ? (_openBlock(), _createElementBlock("div", _hoisted_12, [
          (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(items.value, (row) => {
            return (_openBlock(), _createBlock(_component_VCard, {
              key: `card-${row.raw_title}`,
              border: "",
              variant: "outlined",
              class: "course-review-card"
            }, {
              default: _withCtx(() => [
                _createVNode(_component_VCardTitle, { class: "text-subtitle-1 text-break" }, {
                  default: _withCtx(() => [
                    _createTextVNode(_toDisplayString(row.raw_title), 1)
                  ]),
                  _: 2
                }, 1024),
                _createVNode(_component_VCardText, null, {
                  default: _withCtx(() => [
                    _createElementVNode("div", _hoisted_13, [
                      _createVNode(_component_VChip, {
                        size: "small",
                        variant: "tonal",
                        color: "primary"
                      }, {
                        default: _withCtx(() => [
                          _createTextVNode(_toDisplayString(row.recognition_source_label || '本地'), 1)
                        ]),
                        _: 2
                      }, 1024),
                      _cache[19] || (_cache[19] = _createElementVNode("span", { class: "text-caption text-medium-emphasis" }, "识别来源", -1))
                    ]),
                    _createVNode(_component_VTextField, {
                      modelValue: row.final_title,
                      "onUpdate:modelValue": $event => ((row.final_title) = $event),
                      label: "建议名称",
                      "aria-label": `建议名称：${row.raw_title}`,
                      variant: "outlined",
                      density: "comfortable",
                      autocomplete: "off",
                      disabled: isSaving(row) || isOrganizing(row)
                    }, null, 8, ["modelValue", "onUpdate:modelValue", "aria-label", "disabled"]),
                    (isOrganizing(row))
                      ? (_openBlock(), _createBlock(_component_VProgressLinear, {
                          key: 0,
                          indeterminate: !hasOrganizingValue(),
                          "model-value": fileTransferValue.value || 0,
                          color: "primary",
                          class: "mb-2",
                          "aria-label": "正在整理"
                        }, null, 8, ["indeterminate", "model-value"]))
                      : _createCommentVNode("", true),
                    (isOrganizing(row))
                      ? (_openBlock(), _createElementBlock("div", _hoisted_14, _toDisplayString(organizingStatusText()), 1))
                      : _createCommentVNode("", true),
                    (isOrganizing(row))
                      ? (_openBlock(), _createBlock(_component_VChip, {
                          key: 2,
                          size: "small",
                          variant: "tonal",
                          color: "info",
                          class: "mb-2",
                          "aria-label": "整理中"
                        }, {
                          default: _withCtx(() => [...(_cache[20] || (_cache[20] = [
                            _createTextVNode(" 整理中 ", -1)
                          ]))]),
                          _: 1
                        }))
                      : _createCommentVNode("", true),
                    _createVNode(_component_VBtn, {
                      class: "mb-3",
                      variant: "tonal",
                      "min-width": "132",
                      loading: isTmdbLoading(row),
                      disabled: isSourcePending(row) || isSaving(row) || isTmdbLoading(row) || isOrganizing(row),
                      "aria-label": `按名称搜索 TMDB：${row.raw_title}`,
                      onClick: $event => (searchTmdb(row))
                    }, {
                      default: _withCtx(() => [...(_cache[21] || (_cache[21] = [
                        _createTextVNode(" 按名称搜索 TMDB ", -1)
                      ]))]),
                      _: 1
                    }, 8, ["loading", "disabled", "aria-label", "onClick"]),
                    _cache[27] || (_cache[27] = _createElementVNode("div", { class: "text-caption text-medium-emphasis mt-n2 mb-3" }, "按当前目录名称搜索，无需输入 TMDB ID", -1)),
                    (tmdbCandidatesFor(row).length)
                      ? (_openBlock(), _createBlock(_component_VSheet, {
                          key: 3,
                          border: "",
                          rounded: "",
                          class: "course-tmdb-candidates mb-3 pa-2"
                        }, {
                          default: _withCtx(() => [
                            _cache[22] || (_cache[22] = _createElementVNode("div", { class: "text-caption text-medium-emphasis mb-1" }, "请选择匹配的 TMDB 作品", -1)),
                            (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(tmdbCandidatesFor(row), (candidate) => {
                              return (_openBlock(), _createBlock(_component_VBtn, {
                                key: candidate.candidate_key,
                                block: "",
                                class: "course-tmdb-candidate mb-1",
                                variant: "text",
                                disabled: isSaving(row) || isTmdbLoading(row) || isOrganizing(row),
                                onClick: $event => (associateTmdb(row, candidate))
                              }, {
                                default: _withCtx(() => [
                                  _createTextVNode(_toDisplayString(candidate.title), 1),
                                  (candidate.year)
                                    ? (_openBlock(), _createElementBlock("span", _hoisted_15, "（" + _toDisplayString(candidate.year) + "）", 1))
                                    : _createCommentVNode("", true),
                                  _createTextVNode(" · " + _toDisplayString(candidate.label || candidate.media_type), 1)
                                ]),
                                _: 2
                              }, 1032, ["disabled", "onClick"]))
                            }), 128))
                          ]),
                          _: 2
                        }, 1024))
                      : _createCommentVNode("", true),
                    _createVNode(_component_VSelect, {
                      modelValue: row.target_library,
                      "onUpdate:modelValue": $event => ((row.target_library) = $event),
                      items: libraries.value,
                      "item-title": "title",
                      "item-value": "value",
                      label: "目标媒体库",
                      "aria-label": `目标媒体库：${row.raw_title}`,
                      variant: "outlined",
                      density: "comfortable",
                      disabled: isSaving(row) || isOrganizing(row)
                    }, null, 8, ["modelValue", "onUpdate:modelValue", "items", "aria-label", "disabled"]),
                    (isSourcePending(row))
                      ? (_openBlock(), _createBlock(_component_VAlert, {
                          key: 4,
                          type: "info",
                          density: "compact",
                          variant: "tonal",
                          class: "mb-2",
                          role: "status"
                        }, {
                          default: _withCtx(() => [...(_cache[23] || (_cache[23] = [
                            _createTextVNode(" 源目录暂不稳定，无法执行搜索、关联或整理，请稍后点击刷新重试 ", -1)
                          ]))]),
                          _: 1
                        }))
                      : _createCommentVNode("", true),
                    _createElementVNode("div", {
                      class: "text-body-2 text-medium-emphasis text-break mb-3",
                      title: targetPath(row)
                    }, " 目标位置：" + _toDisplayString(targetPath(row)), 9, _hoisted_16),
                    _createElementVNode("div", _hoisted_17, [
                      _createVNode(_component_VChip, {
                        size: "small",
                        variant: "tonal"
                      }, {
                        default: _withCtx(() => [
                          _createTextVNode(_toDisplayString(libraryLabel(row)), 1)
                        ]),
                        _: 2
                      }, 1024),
                      _createVNode(_component_VChip, {
                        size: "small",
                        variant: "tonal",
                        color: statusChipColor(row)
                      }, {
                        default: _withCtx(() => [
                          _createTextVNode(_toDisplayString(row.status_label || '需要确认'), 1)
                        ]),
                        _: 2
                      }, 1032, ["color"]),
                      _createVNode(_component_VSpacer),
                      _createVNode(_component_VBtn, {
                        color: "primary",
                        variant: "tonal",
                        "min-width": "108",
                        loading: isOrganizing(row),
                        disabled: Boolean(organizingKey.value) || !canConfirm(row) || isTmdbLoading(row),
                        "aria-label": `确认整理：${row.raw_title}`,
                        onClick: $event => (saveReview(row, 'confirm'))
                      }, {
                        default: _withCtx(() => [...(_cache[24] || (_cache[24] = [
                          _createTextVNode(" 保存并整理 ", -1)
                        ]))]),
                        _: 1
                      }, 8, ["loading", "disabled", "aria-label", "onClick"]),
                      (row.status_label !== '已跳过')
                        ? (_openBlock(), _createBlock(_component_VBtn, {
                            key: 0,
                            variant: "text",
                            "min-width": "76",
                            disabled: isSourcePending(row) || isSaving(row) || isOrganizing(row),
                            "aria-label": `跳过：${row.raw_title}`,
                            onClick: $event => (saveReview(row, 'ignore'))
                          }, {
                            default: _withCtx(() => [...(_cache[25] || (_cache[25] = [
                              _createTextVNode(" 跳过 ", -1)
                            ]))]),
                            _: 1
                          }, 8, ["disabled", "aria-label", "onClick"]))
                        : (_openBlock(), _createBlock(_component_VBtn, {
                            key: 1,
                            variant: "text",
                            "min-width": "76",
                            disabled: isSourcePending(row) || Boolean(organizingKey.value) || !canConfirm(row) || isTmdbLoading(row),
                            "aria-label": `重新确认：${row.raw_title}`,
                            onClick: $event => (saveReview(row, 'confirm'))
                          }, {
                            default: _withCtx(() => [...(_cache[26] || (_cache[26] = [
                              _createTextVNode(" 重新确认 ", -1)
                            ]))]),
                            _: 1
                          }, 8, ["disabled", "aria-label", "onClick"]))
                    ])
                  ]),
                  _: 2
                }, 1024)
              ]),
              _: 2
            }, 1024))
          }), 128))
        ]))
      : _createCommentVNode("", true)
  ]))
}
}

};
const Page = /*#__PURE__*/_export_sfc(_sfc_main, [['__scopeId',"data-v-e02dfcb2"]]);

export { Page as default };
