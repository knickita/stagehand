using System.Collections;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Rendering;

public class Selectable : MonoBehaviour
{
    public bool selected=false;
    public bool isArray;
    
    // Start is called before the first frame update
    void Start()
    {
    }

    public bool IsSelected(){
        return selected;
    }

    public void Select(bool select){
        Renderer renderer = GetComponent<Renderer>();
        if(renderer != null){
            renderer.material.SetFloat("_Selected", select ? 1f : 0f);
        }
        selected=select;
    }
}
