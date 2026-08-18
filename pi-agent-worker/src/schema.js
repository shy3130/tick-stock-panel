// JSON Schema (as sent by the Python runtime) to TypeBox conversion.
//
// The Python side owns tool definitions and only forwards a JSON Schema
// object per tool. Pi/TypeBox require a TypeBox schema, so we translate the
// subset of JSON Schema that the agent tools actually use. Anything outside
// that subset must fail closed: returning a wrong-but-accepted schema would
// silently corrupt tool argument validation.

/** Thrown when a schema cannot be converted safely. */
export class SchemaConversionError extends Error {
  constructor(message) {
    super(message);
    this.name = "SchemaConversionError";
  }
}

const KNOWN_PRIMITIVES = new Set(["string", "number", "integer", "boolean", "null"]);

function fail(reason) {
  throw new SchemaConversionError(reason);
}

function asOptionalArray(value, what) {
  if (value === undefined) return undefined;
  if (!Array.isArray(value)) fail(`${what} must be an array`);
  return value;
}

/**
 * Convert a Python JSON Schema definition into a TypeBox schema.
 * Returns a plain typebox schema object.
 */
export function jsonSchemaToTypeBox(schema, what = "schema") {
  if (schema === null || typeof schema !== "object" || Array.isArray(schema)) {
    fail(`${what} must be a JSON object`);
  }
  return convertNode(schema, what);
}

const UNSUPPORTED_KEYWORDS = [
  "$ref",
  "$defs",
  "allOf",
  "anyOf",
  "oneOf",
  "not",
  "if",
  "then",
  "else",
  "propertyNames",
  "patternProperties",
  "dependencies",
  "dependentRequired",
  "dependentSchemas",
  "contains",
  "prefixItems",
  "unevaluatedProperties",
  "unevaluatedItems",
];

function convertNode(node, path) {
  if (node === null || typeof node !== "object" || Array.isArray(node)) {
    fail(`${path} must be an object`);
  }

  if (node.const !== undefined) {
    const t = node.const === null ? "null" : typeof node.const;
    if (!["string", "number", "boolean"].includes(t)) {
      fail(`${path}.const must be a primitive value`);
    }
    return { ...metaOf(node), const: node.const };
  }

  if (node.enum !== undefined) {
    if (!Array.isArray(node.enum) || node.enum.length === 0) {
      fail(`${path}.enum must be a non-empty array`);
    }
    for (const value of node.enum) {
      const t = value === null ? "null" : typeof value;
      if (!["string", "number", "boolean"].includes(t)) {
        fail(`${path}.enum entries must be strings, numbers, booleans or null`);
      }
    }
    return { ...metaOf(node), enum: [...node.enum] };
  }

  for (const keyword of UNSUPPORTED_KEYWORDS) {
    if (node[keyword] !== undefined) {
      fail(`${path} uses unsupported keyword "${keyword}"`);
    }
  }

  const type = node.type;

  if (typeof type === "string") {
    switch (type) {
      case "object":
        return convertObject(node, path);
      case "array":
        return convertArray(node, path);
      case "string":
        return convertString(node, path);
      case "number":
        return convertNumber(node, path, false);
      case "integer":
        return convertNumber(node, path, true);
      case "boolean":
        return { ...metaOf(node), type: "boolean" };
      case "null":
        return { ...metaOf(node), type: "null" };
      default:
        fail(`${path} has unsupported type "${type}"`);
    }
  }

  if (Array.isArray(type)) {
    // Union of distinct primitive types. Constraints (minLength, minimum, ...)
    // on a type-array schema apply ambiguously across members, so fail closed.
    for (const constraint of ["minLength", "maxLength", "pattern", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minItems", "maxItems", "uniqueItems"]) {
      if (node[constraint] !== undefined) {
        fail(`${path} type arrays with constraints are not supported`);
      }
    }
    for (const member of type) {
      if (typeof member !== "string" || !KNOWN_PRIMITIVES.has(member)) {
        fail(`${path}.type entries must be primitive type names`);
      }
    }
    const set = new Set(type);
    const nullable = set.delete("null");
    const rest = [...set];
    if (rest.length === 0) {
      return { ...metaOf(node), type: "null" };
    }
    if (rest.length === 1) {
      const schema = { ...metaOf(node), type: rest[0] };
      if (nullable) schema.type = [rest[0], "null"];
      return schema;
    }
    const schema = { ...metaOf(node), anyOf: rest.map((t) => ({ type: t })) };
    if (nullable) schema.anyOf.push({ type: "null" });
    return schema;
  }

  // No explicit type. Infer object/array shapes only when their exclusive
  // keywords are present; anything else is ambiguous and fails closed.
  if (node.properties !== undefined || node.required !== undefined || node.additionalProperties !== undefined) {
    return convertObject(node, path);
  }
  if (node.items !== undefined || node.minItems !== undefined || node.maxItems !== undefined) {
    return convertArray(node, path);
  }
  const unconstrainedKeys = Object.keys(node).filter(
    (key) => key !== "description" && key !== "default",
  );
  if (unconstrainedKeys.length === 0) {
    // The condition screener's `value` deliberately accepts any JSON value.
    // TypeBox Compile treats an empty schema as unconstrained.
    return metaOf(node);
  }
  fail(`${path} is missing a supported "type" keyword`);
}

function metaOf(node) {
  const out = {};
  if (typeof node.description === "string" && node.description.length > 0) {
    out.description = node.description;
  }
  if (node.default !== undefined) {
    out.default = node.default;
  }
  return out;
}

function convertObject(node, path) {
  const properties = node.properties;
  if (properties === undefined) {
    if (node.additionalProperties === undefined) {
      fail(`${path} object schema must declare "properties"`);
    }
    if (node.additionalProperties === true) {
      return { ...metaOf(node), type: "object" };
    }
    if (node.additionalProperties === false) {
      // "closed" empty object: no properties declared, nothing else allowed.
      return { ...metaOf(node), type: "object", properties: {}, additionalProperties: false };
    }
    if (node.additionalProperties === null || typeof node.additionalProperties !== "object" || Array.isArray(node.additionalProperties)) {
      fail(`${path}.additionalProperties must be a boolean or schema object`);
    }
    return convertMap(node, path);
  }
  if (properties === null || typeof properties !== "object" || Array.isArray(properties)) {
    fail(`${path}.properties must be an object`);
  }
  const required = asOptionalArray(node.required, `${path}.required`) ?? [];
  for (const key of required) {
    if (typeof key !== "string") fail(`${path}.required entries must be strings`);
    if (!(key in properties)) {
      fail(`${path}.required lists "${key}" which is not in "properties"`);
    }
  }
  const ap = node.additionalProperties;
  if (ap !== undefined && ap !== false && ap !== true && (ap === null || typeof ap !== "object" || Array.isArray(ap))) {
    fail(`${path}.additionalProperties must be a boolean or schema object`);
  }
  if (node.minProperties !== undefined || node.maxProperties !== undefined) {
    fail(`${path} object minProperties/maxProperties are not supported`);
  }
  // TypeBox 1.3.7 schemas are plain standard JSON Schema: required names are
  // listed on the object itself and optional properties carry no marker.
  const out = { ...metaOf(node), type: "object", properties: {} };
  const requiredSet = new Set(required);
  const requiredNames = [];
  for (const [key, sub] of Object.entries(properties)) {
    out.properties[key] = convertNode(sub, `${path}.properties["${key}"]`);
    if (requiredSet.has(key)) {
      requiredNames.push(key);
    }
  }
  if (requiredNames.length > 0) {
    out.required = requiredNames;
  }
  if (ap === true) {
    out.additionalProperties = true;
  } else if (typeof ap === "object" && ap !== null && !Array.isArray(ap)) {
    // Record-style: declared keys validated plus any extra keys against ap.
    out.additionalProperties = convertNode(ap, `${path}.additionalProperties`);
  } else {
    // Tool arguments are closed by default. Python owns the allowlist and
    // must opt in explicitly before a model may invent extra parameters.
    out.additionalProperties = false;
  }
  return out;
}

function convertMap(node, path) {
  const converted = convertNode(node.additionalProperties, `${path}.additionalProperties`);
  return {
    ...metaOf(node),
    type: "object",
    additionalProperties: converted,
  };
}

function convertArray(node, path) {
  const out = { ...metaOf(node), type: "array" };
  if (node.items !== undefined) {
    if (Array.isArray(node.items)) {
      fail(`${path} tuple-form "items" is not supported`);
    }
    if (node.items === null || typeof node.items !== "object") {
      fail(`${path}.items must be a schema object`);
    }
    out.items = convertNode(node.items, `${path}.items`);
  }
  if (node.minItems !== undefined) out.minItems = node.minItems;
  if (node.maxItems !== undefined) out.maxItems = node.maxItems;
  if (node.uniqueItems === true) out.uniqueItems = true;
  return out;
}

function convertString(node, path) {
  const out = { ...metaOf(node), type: "string" };
  if (node.minLength !== undefined) out.minLength = node.minLength;
  if (node.maxLength !== undefined) out.maxLength = node.maxLength;
  if (typeof node.pattern === "string") out.pattern = node.pattern;
  const format = node.format;
  if (format !== undefined) {
    if (!["date-time", "date", "time", "email", "uuid", "uri"].includes(format)) {
      fail(`${path} has unsupported string format ${JSON.stringify(format)}`);
    }
    out.format = format;
  }
  return out;
}

function convertNumber(node, path, isInteger) {
  const out = { ...metaOf(node), type: isInteger ? "integer" : "number" };
  if (node.minimum !== undefined) out.minimum = node.minimum;
  if (node.maximum !== undefined) out.maximum = node.maximum;
  if (node.exclusiveMinimum !== undefined) out.exclusiveMinimum = node.exclusiveMinimum;
  if (node.exclusiveMaximum !== undefined) out.exclusiveMaximum = node.exclusiveMaximum;
  if (node.multipleOf !== undefined) out.multipleOf = node.multipleOf;
  return out;
}
