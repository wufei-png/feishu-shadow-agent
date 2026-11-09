import { copyFor } from "./copy";
import { chineseCatalogResources } from "./catalog-zh";

export const zhCN = { ...copyFor(0), ...chineseCatalogResources() };
