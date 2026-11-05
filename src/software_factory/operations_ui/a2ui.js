// Render the Basic Catalog subset used by the blocked-plan answer surface.
const version = "v0.9.1";
const catalog = "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json";

export function renderAttention(messages, drafts, onAction) {
  const surface = messages[0]?.createSurface;
  const update = messages[1]?.updateComponents;
  const model = messages[2]?.updateDataModel;
  if (messages.length !== 3 || messages.some(message => message.version !== version) ||
      !surface || surface.catalogId !== catalog || update?.surfaceId !== surface.surfaceId ||
      model?.surfaceId !== surface.surfaceId) {
    throw new Error("A2UI_SURFACE_INVALID");
  }

  const components = new Map(update.components.map(component => [component.id, component]));
  const root = components.get("root");
  if (root?.component !== "Column") throw new Error("A2UI_SURFACE_INVALID");
  const data = model.value;
  if (drafts.surfaceId === surface.surfaceId) {
    for (const [id, answer] of Object.entries(data.answers)) {
      const previous = drafts.answers[id];
      if (previous) {
        answer.text = previous.text;
        answer.choice = previous.choice;
      }
    }
  }
  drafts.surfaceId = surface.surfaceId;
  drafts.answers = data.answers;

  function location(path) {
    const keys = path.split("/").slice(1);
    const key = keys.pop();
    let container = data;
    for (const part of keys) {
      if (!container || !Object.hasOwn(container, part)) throw new Error("A2UI_BINDING_INVALID");
      container = container[part];
    }
    if (!container || !Object.hasOwn(container, key)) throw new Error("A2UI_BINDING_INVALID");
    return {container, key};
  }

  function resolve(value) {
    if (value && typeof value === "object" && Object.hasOwn(value, "path")) {
      const {container, key} = location(value.path);
      return container[key];
    }
    return value;
  }

  const form = document.createElement("div");
  form.className = "attention-form a2ui-attention";
  for (const id of root.children) {
    const item = components.get(id);
    switch (item?.component) {
      case "Text": {
        const text = document.createElement(item.variant === "h4" ? "h4" : "p");
        text.textContent = resolve(item.text);
        form.append(text);
        break;
      }
      case "TextField": {
        const {container, key} = location(item.value.path);
        const label = document.createElement("label");
        label.textContent = item.label;
        const input = document.createElement("textarea");
        input.rows = 3;
        input.dataset.a2uiId = item.id;
        input.maxLength = 1200;
        input.value = container[key];
        input.addEventListener("input", () => { container[key] = input.value; });
        const updateVisibility = () => {
          const choice = container.choice[0];
          label.hidden = !choice;
          input.required = choice === "other";
        };
        form.addEventListener("input", updateVisibility);
        updateVisibility();
        label.append(input);
        form.append(label);
        break;
      }
      case "ChoicePicker": {
        if (item.variant !== "mutuallyExclusive") throw new Error("A2UI_COMPONENT_INVALID");
        const {container, key} = location(item.value.path);
        const options = document.createElement("fieldset");
        const legend = document.createElement("legend");
        legend.textContent = resolve(item.label);
        options.append(legend);
        if (!item.options.some(option => container[key].includes(option.value))) container[key] = [];
        for (const option of item.options) {
          const label = document.createElement("label");
          const input = document.createElement("input");
          input.type = "radio";
          input.name = `${surface.surfaceId}-${item.id}`;
          input.value = option.value;
          input.checked = container[key].includes(option.value);
          input.addEventListener("input", () => { container[key] = [option.value]; });
          label.append(input, document.createTextNode(option.label));
          options.append(label);
        }
        form.append(options);
        break;
      }
      case "Button": {
        const event = item.action?.event;
        if (!["continue_interview", "adopt_specification", "refine_specification"].includes(event?.name) || item.id !== event.name) {
          throw new Error("A2UI_ACTION_INVALID");
        }
        const button = document.createElement("button");
        button.type = "button";
        button.classList.toggle("primary", item.variant === "primary");
        button.textContent = resolve(components.get(item.child)?.text);
        button.addEventListener("click", async () => {
          if (Object.hasOwn(event.context, "answers")) {
            for (const input of form.querySelectorAll("textarea:required")) {
              if (!input.reportValidity()) return;
            }
          }
          const context = Object.fromEntries(
            Object.entries(event.context).map(([name, value]) => [name, resolve(value)]),
          );
          const action = {
            name: event.name, surfaceId: surface.surfaceId, sourceComponentId: item.id,
            timestamp: new Date().toISOString(), context,
          };
          const buttons = form.querySelectorAll("button");
          for (const control of buttons) control.disabled = true;
          try {
            await onAction({version, action});
          } finally {
            for (const control of buttons) control.disabled = false;
          }
        });
        form.append(button);
        break;
      }
      default:
        throw new Error("A2UI_COMPONENT_INVALID");
    }
  }
  return form;
}
